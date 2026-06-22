"""Impala metadata browser screen with split-panel layout."""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static
from textual.worker import Worker

from .. import impala
from .confirm import ConfirmScreen
from .sidebar import Sidebar


NO_TABLES_PLACEHOLDER = "(no tables)"


class BrowserScreen(Screen[None]):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("enter", "describe", "Describe"),
        ("d", "drop", "Drop"),
        ("s", "show_tables", "Load Tables"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self, *, auto_load: bool = True) -> None:
        super().__init__()
        self._auto_load = auto_load
        self._tables: list[str] = []
        self._describe_text: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        sidebar = Sidebar()
        sidebar.active_screen = "browse"
        yield sidebar
        with Vertical(id="main-content"):
            with Vertical(id="browser-content"):
                yield Static("[bold]Browse Impala Metadata[/]", classes="section-title")

                with Horizontal(id="browser-split"):
                    with Vertical(id="browser-left"):
                        yield Static("[dim]Schema \u00b7 table filter[/]", classes="input-caption")
                        with Horizontal(id="browser-query-row"):
                            yield Input(value="aa_enc", placeholder="Schema", id="schema")
                            yield Input(value="*", placeholder="Filter (e.g. dispatch_*)", id="filter")
                            yield Button("Load Tables [S]", id="show", variant="default")
                        yield DataTable(id="browser-table")
                        with Horizontal(id="browser-status"):
                            yield Static("", id="browser-selected")
                            yield Static("", id="browser-count")

                    with Vertical(id="browser-right"):
                        yield Static("", id="file-preview-title")
                        yield Static("", id="file-preview-path")
                        with Vertical(id="file-meta"):
                            yield Static("", id="meta-info")
                        with Vertical(id="file-preview-code"):
                            yield DataTable(id="describe-table")
                            yield Static("", id="describe-body")

            with Horizontal(classes="action-bar"):
                yield Static("", id="browser-action-status", classes="action-status")
                yield Button("Back [B]", id="back", variant="default")
                yield Button("Describe [Enter]", id="describe", variant="primary")
                yield Button("Drop [D]", id="drop", variant="error")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#browser-table", DataTable)
        table.add_columns("Name", "Type")
        table.cursor_type = "row"
        describe_table = self.query_one("#describe-table", DataTable)
        describe_table.add_columns("Column", "Type", "Comment")
        describe_table.show_cursor = False
        describe_table.display = False
        self._show_detail_placeholder()
        self._update_action_state()
        if self._auto_load:
            await self.action_show_tables()

    def _show_detail_placeholder(self) -> None:
        self.query_one("#file-preview-title", Static).update("[dim]No table selected[/]")
        self.query_one("#file-preview-path", Static).update("")
        self.query_one("#meta-info", Static).update("")
        self.query_one("#browser-selected", Static).update("")
        self._show_detail_message(
            "Select a table and press Enter to view its schema.",
            severity="dim",
        )

    def _show_table_list_message(self, message: str, severity: str = "info") -> None:
        self.query_one("#file-preview-title", Static).update("[dim]Table list[/]")
        self.query_one("#file-preview-path", Static).update("")
        self.query_one("#meta-info", Static).update(
            f"[dim]Schema: {self._schema()}[/]" if self._schema() else ""
        )
        self.query_one("#browser-selected", Static).update("")
        self._show_detail_message(message, severity=severity)

    def _show_detail_message(self, message: str, severity: str = "info") -> None:
        color = {
            "dim": "dim",
            "info": "cyan",
            "success": "green",
            "error": "red",
        }.get(severity, "dim")
        body = self.query_one("#describe-body", Static)
        body.update(f"[{color}]{message}[/]")
        body.display = True
        self.query_one("#describe-table").display = False

    def _schema(self) -> str:
        return self.query_one("#schema", Input).value.strip()

    def _selected_table(self) -> str:
        table_widget = self.query_one("#browser-table", DataTable)
        try:
            row_key = table_widget.get_row_at(table_widget.cursor_row)
            return str(row_key[0])
        except Exception:
            return ""

    def _full_table(self) -> str:
        selected = self._selected_table()
        # The empty-schema placeholder row is not a real table; never qualify or
        # act on it, otherwise DESCRIBE/DROP would target "schema.(no tables)".
        if not selected or selected == NO_TABLES_PLACEHOLDER:
            return ""
        return selected if "." in selected else f"{self._schema()}.{selected}"

    def _update_action_state(self) -> None:
        """Enable/disable DESCRIBE and DROP based on whether a real table is selected.

        When a schema has no tables the list shows a single ``(no tables)``
        placeholder row; actions must stay disabled so DESCRIBE/DROP are never
        run against the placeholder.
        """
        has_selection = bool(self._full_table())
        self.query_one("#describe", Button).disabled = not has_selection
        self.query_one("#drop", Button).disabled = not has_selection

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "show":
            await self.action_show_tables()
        elif event.button.id == "describe":
            await self.action_describe()
        elif event.button.id == "drop":
            self.action_drop()
        elif event.button.id == "back":
            self.app.pop_screen()

    async def action_show_tables(self, *, describe_selection: bool = True) -> None:
        self._show_table_list_message("Loading tables…", severity="dim")
        try:
            schema = self._schema()
            filter_val = self.query_one("#filter", Input).value.strip() or "*"
            self._tables = await impala.show_tables(schema, filter_val)
        except Exception as exc:
            self._show_table_list_message(str(exc), severity="error")
            self.notify(f"SHOW TABLES failed: {exc}", severity="error")
            return

        table = self.query_one("#browser-table", DataTable)
        table.clear()
        for name in self._tables:
            table.add_row(name, "table")
        self.query_one("#browser-count", Static).update(
            f"[dim]{len(self._tables)} tables[/]"
        )
        if not self._tables:
            table.add_row(NO_TABLES_PLACEHOLDER, "")
            self._show_detail_placeholder()
        elif describe_selection:
            table.cursor_coordinate = (0, 0)
            await self.action_describe()
        self._update_action_state()

    async def action_describe(self) -> None:
        full = self._full_table()
        if not full:
            return
        self.query_one("#describe-body", Static).update("[dim]Loading schema\u2026[/]")
        self.query_one("#describe-body").display = True
        self.query_one("#describe-table").display = False
        try:
            result = await impala.describe_table(full)
        except Exception as exc:
            result = str(exc)

        self._describe_text = result
        self.query_one("#file-preview-title", Static).update(
            f"[bold cyan]{full}[/]"
        )
        self.query_one("#file-preview-path", Static).update("")

        columns = self._parse_describe(result)
        col_count = len(columns)
        self.query_one("#meta-info", Static).update(
            f"[dim]Impala Table \u00b7 {col_count} columns \u00b7 Schema: {self._schema()}[/]"
        )

        if columns:
            dt = self.query_one("#describe-table", DataTable)
            dt.clear()
            for col in columns:
                dt.add_row(col["name"], col["type"], col["comment"])
            self.query_one("#describe-body").display = False
            dt.display = True
        else:
            self.query_one("#describe-body", Static).update(result)
            self.query_one("#describe-body").display = True
            self.query_one("#describe-table").display = False

        self.query_one("#browser-selected", Static).update(
            f"[cyan]Selected: {full}[/]"
        )
        self._update_action_state()

    @staticmethod
    def _parse_describe(raw: str) -> list[dict[str, str]]:
        """Parse pipe-delimited DESCRIBE output into column dicts."""
        columns = []
        for line in raw.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if parts[:3] == ["name", "type", "comment"]:
                continue  # impala-shell header row, not a real column
            if len(parts) >= 2:
                columns.append({
                    "name": parts[0],
                    "type": parts[1],
                    "comment": parts[2] if len(parts) > 2 else "",
                })
        return columns

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update button state whenever the cursor moves to a different row."""
        self._update_action_state()

    def action_cursor_down(self) -> None:
        if self.query_one("#browser-table", DataTable).has_focus:
            self.query_one("#browser-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.query_one("#browser-table", DataTable).has_focus:
            self.query_one("#browser-table", DataTable).action_cursor_up()

    def action_drop(self) -> "Worker[None]":
        """Run the confirm-and-drop flow in a worker (see NewJobScreen.action_launch)."""
        return self.run_worker(self._drop_flow(), name="drop-flow", exclusive=True)

    async def _drop_flow(self) -> None:
        full = self._full_table()
        if not full:
            return

        confirmed = await self._confirm_drop(full)
        if not confirmed:
            return
        try:
            result = await impala.drop_table(full)
            self.notify(f"Dropped {full}", severity="information")
            await self.action_show_tables(describe_selection=False)
            self._show_detail_message(result, severity="success")
        except Exception as exc:
            self._show_detail_message(str(exc), severity="error")
            self.notify(f"DROP failed: {exc}", severity="error")

    async def _confirm_drop(self, full_table: str) -> bool:
        loop_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def on_result(result: bool | None) -> None:
            if not loop_future.done():
                loop_future.set_result(bool(result))

        self.app.push_screen(
            ConfirmScreen(
                "DROP TABLE",
                (
                    f"Drop [cyan]{full_table}[/]?\n\n"
                    "[red]This cannot be undone.[/]\n"
                    "Type the full table name to confirm."
                ),
                danger=True,
                confirm_label="Drop",
                cancel_label="Keep Table",
                required_confirmation_text=full_table,
            ),
            callback=on_result,
        )
        return await loop_future

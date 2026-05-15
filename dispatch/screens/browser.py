"""Impala metadata browser screen with split-panel layout."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from .. import impala
from .sidebar import Sidebar


class BrowserScreen(Screen[None]):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("enter", "describe", "Describe"),
        ("d", "drop", "Drop"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._tables: list[str] = []
        self._describe_text: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        sidebar = Sidebar()
        sidebar.active_screen = "browse"
        yield sidebar
        with Vertical(id="main-content"):
            with Vertical(id="browser-content"):
                yield Static("Browse Impala Metadata", classes="section-title")

                with Horizontal(id="browser-split"):
                    with Vertical(id="browser-left"):
                        with Horizontal(id="search-row"):
                            yield Input(value="dw_settle", placeholder="Schema", id="schema")
                            yield Input(value="*", placeholder="Filter pattern", id="filter")
                        yield Button("SHOW TABLES", id="show", variant="primary")
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
                            yield Static("", id="describe-body")

                with Horizontal(classes="button-row"):
                    yield Button("DESCRIBE [Enter]", id="describe", variant="primary")
                    yield Button("DROP [D]", id="drop", variant="error")
                    yield Button("Back [B]", id="back", variant="default")

                yield Static(
                    "[dim]Use arrow keys to navigate, Enter to describe, D to drop[/]",
                    id="browser-help",
                )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#browser-table", DataTable)
        table.add_columns("Name", "Type")
        table.cursor_type = "row"

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
        if not selected:
            return ""
        return selected if "." in selected else f"{self._schema()}.{selected}"

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "show":
            await self.action_show_tables()
        elif event.button.id == "describe":
            await self.action_describe()
        elif event.button.id == "drop":
            await self.action_drop()
        elif event.button.id == "back":
            self.app.pop_screen()

    async def action_show_tables(self) -> None:
        try:
            schema = self._schema()
            filter_val = self.query_one("#filter", Input).value.strip() or "*"
            self._tables = await impala.show_tables(schema, filter_val)
        except Exception as exc:
            self.query_one("#describe-body", Static).update(f"[red]{exc}[/]")
            return

        table = self.query_one("#browser-table", DataTable)
        table.clear()
        for name in self._tables:
            table.add_row(name, "table")
        self.query_one("#browser-count", Static).update(
            f"[dim]{len(self._tables)} items[/]"
        )
        if not self._tables:
            table.add_row("(no tables)", "")

    async def action_describe(self) -> None:
        full = self._full_table()
        if not full:
            return
        try:
            result = await impala.describe_table(full)
        except Exception as exc:
            result = str(exc)

        self._describe_text = result
        self.query_one("#file-preview-title", Static).update(
            f"[bold cyan]{full}[/]"
        )
        self.query_one("#file-preview-path", Static).update(
            f"[dim]{self._schema()}.{self._selected_table()}[/]"
        )
        self.query_one("#meta-info", Static).update(
            f"[dim]Type: Impala Table   Schema: {self._schema()}[/]"
        )

        lines = result.splitlines()
        width = len(str(len(lines)))
        numbered = []
        for i, line in enumerate(lines, 1):
            numbered.append(f"[dim]{i:>{width}}[/] \u2502 {line}")
        self.query_one("#describe-body", Static).update("\n".join(numbered))
        self.query_one("#browser-selected", Static).update(
            f"[cyan]Selected: {full}[/]"
        )

    async def action_drop(self) -> None:
        full = self._full_table()
        if not full:
            return
        try:
            result = await impala.drop_table(full)
            self.query_one("#describe-body", Static).update(f"[green]{result}[/]")
        except Exception as exc:
            self.query_one("#describe-body", Static).update(f"[red]{exc}[/]")

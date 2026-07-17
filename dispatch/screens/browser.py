"""Impala metadata browser screen with split-panel layout."""

from __future__ import annotations

from typing import TypedDict

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.geometry import Size
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static
from textual.widgets.data_table import CellDoesNotExist, ColumnKey
from textual.worker import Worker

from .. import capacity, impala
from .confirm import ConfirmScreen
from .sidebar import Sidebar

NO_TABLES_PLACEHOLDER = "(no tables)"
# Use capital X: Rich markup treats lowercase "[x]" as a style tag and renders it blank.
CHECKED_MARKER = Text("[X]", style="bold green")
UNCHECKED_MARKER = Text("[ ]")
SIZE_PENDING = "…"
SIZE_UNKNOWN = "—"

# Fixed Browse-list column widths. Name flexes into the leftover space so the
# Size column stays on-screen at typical SSH widths (see _sync_column_widths).
_SEL_COLUMN_WIDTH = 5
_TYPE_COLUMN_WIDTH = 5
_SIZE_COLUMN_WIDTH = 10
_NAME_COLUMN_MIN_WIDTH = 4
_COLUMN_COUNT = 4
_SEL_COLUMN_INDEX = 0


class _TableRow(TypedDict):
    name: str
    type: str
    size_display: str
    size_bytes: int | None


class BrowserTable(DataTable):
    """DataTable that toggles row checks when the Sel checkbox cell is clicked."""

    class SelClicked(Message):
        """Posted when the user clicks the Sel checkbox for a data row."""

        def __init__(self, data_table: DataTable, row_key: object) -> None:
            self.data_table = data_table
            self.row_key = row_key
            super().__init__()

        @property
        def control(self) -> DataTable:
            return self.data_table

    async def _on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        if meta and "row" in meta and "column" in meta:
            row_index = meta["row"]
            column_index = meta["column"]
            if (
                column_index == _SEL_COLUMN_INDEX
                and isinstance(row_index, int)
                and row_index >= 0
                and row_index < self.row_count
            ):
                row_key = self.coordinate_to_cell_key(Coordinate(row_index, column_index)).row_key
                self.cursor_coordinate = Coordinate(row_index, column_index)
                self.post_message(self.SelClicked(self, row_key))
                event.stop()
                return
        # Textual dispatches the inherited DataTable handler after this one.
        # Calling it here would post header-selection messages twice.


class BrowserScreen(Screen[None]):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("enter", "describe", "Describe"),
        ("d", "drop", "Drop"),
        ("s", "show_tables", "Load Tables"),
        ("o", "cycle_sort", "Sort"),
        ("x", "toggle_check", "Select"),
        ("a", "select_all", "Select All"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    SORT_MODES = ("name", "size")

    def __init__(self, *, auto_load: bool = True) -> None:
        super().__init__()
        self._auto_load = auto_load
        self._tables: list[str] = []
        self._table_rows: list[_TableRow] = []
        self._sort_mode = "name"
        self._sort_reverse = False
        self._checked: set[str] = set()
        self._describe_text: str = ""
        self._sizes_loading = False
        self._size_worker_generation = 0
        self._size_column_key: ColumnKey | None = None

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
                        with Horizontal(id="browser-query-row"):
                            with Vertical(classes="browser-field"):
                                yield Static("Schema", classes="field-label")
                                yield Input(value="aa_enc", placeholder="Schema", id="schema")
                            with Vertical(classes="browser-field", id="browser-filter-field"):
                                with Horizontal(classes="browser-field-header"):
                                    yield Static("Table name", classes="field-label")
                                    yield Static(
                                        "type firstword* and load",
                                        classes="field-hint",
                                    )
                                yield Input(
                                    value="*",
                                    placeholder="Filter (e.g. dispatch_*)",
                                    id="filter",
                                )
                            yield Button("Load Tables [S]", id="show", variant="default")
                        with Horizontal(id="browser-select-row"):
                            yield Button("Select All [A]", id="select-all", variant="default")
                            yield Static("", id="browser-selection-count")
                        yield Static("[dim]Sorted by: name \u2191[/]", id="browser-sort-indicator")
                        yield BrowserTable(id="browser-table")
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
        table = self.query_one("#browser-table", BrowserTable)
        # Name then Size: analysts scan names first; Size stays immediately to the right.
        table.add_column("Sel", width=_SEL_COLUMN_WIDTH)
        table.add_column("Name", width=_NAME_COLUMN_MIN_WIDTH)
        self._size_column_key = table.add_column("Size", width=_SIZE_COLUMN_WIDTH)
        table.add_column("Type", width=_TYPE_COLUMN_WIDTH)
        table.cursor_type = "row"
        self._sync_column_widths()
        describe_table = self.query_one("#describe-table", DataTable)
        describe_table.add_columns("Column", "Type", "Comment")
        describe_table.show_cursor = False
        describe_table.display = False
        self._show_detail_placeholder()
        self._update_action_state()
        if self._auto_load:
            await self.action_show_tables()

    def on_resize(self) -> None:
        """Keep column budget correct when the split pane or terminal changes."""
        self._sync_column_widths()

    def _sync_column_widths(self) -> None:
        """Cap Name so Sel/Size/Type fit; long names truncate instead of clipping Size."""
        table = self.query_one("#browser-table", DataTable)
        if not table.columns:
            return
        available = table.size.width
        if available <= 0:
            return
        # Always reserve the vertical scrollbar gutter so the first layout pass
        # does not give Name a cell that disappears when the scrollbar appears.
        available -= table.scrollbar_size_vertical
        # DataTable render width is content width + 2 * cell_padding per column.
        padding_total = 2 * table.cell_padding * _COLUMN_COUNT
        fixed = _SEL_COLUMN_WIDTH + _TYPE_COLUMN_WIDTH + _SIZE_COLUMN_WIDTH + padding_total
        name_width = max(_NAME_COLUMN_MIN_WIDTH, available - fixed)
        # Column order: Sel, Name, Size, Type
        widths = (
            _SEL_COLUMN_WIDTH,
            name_width,
            _SIZE_COLUMN_WIDTH,
            _TYPE_COLUMN_WIDTH,
        )
        for column, width in zip(table.columns.values(), widths, strict=True):
            column.width = width
            column.auto_width = False
        total_width = sum(column.get_render_width(table) for column in table.columns.values())
        table.virtual_size = Size(total_width, table.virtual_size.height)
        table.refresh()

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

    def _show_capacity_error(
        self,
        operation: str,
        error: capacity.CapacityBusy | capacity.CapacityTimeout | capacity.CapacityLedgerError,
    ) -> None:
        message = f"{operation} failed: {error}"
        self.query_one("#browser-action-status", Static).update(f"[red]{message}[/]")
        self._show_detail_message(message, severity="error")
        self.notify(message, severity="error")

    def _schema(self) -> str:
        return self.query_one("#schema", Input).value.strip()

    def _qualify_table(self, name: str) -> str:
        if not name or name == NO_TABLES_PLACEHOLDER:
            return ""
        return name if "." in name else f"{self._schema()}.{name}"

    def _selected_table(self) -> str:
        table_widget = self.query_one("#browser-table", DataTable)
        try:
            cell_key = table_widget.coordinate_to_cell_key(table_widget.cursor_coordinate)
            name = str(cell_key.row_key.value)
            return name if name in self._tables else ""
        except Exception:
            return ""

    def _full_table(self) -> str:
        return self._qualify_table(self._selected_table())

    def _checked_full_tables(self) -> list[str]:
        return sorted(self._qualify_table(name) for name in self._checked if name in self._tables)

    def _check_marker(self, name: str) -> Text:
        return CHECKED_MARKER if name in self._checked else UNCHECKED_MARKER

    def _update_selection_status(self) -> None:
        count = len(self._checked)
        if count:
            text = f"[dim]{count} selected for drop[/]"
        else:
            text = "[dim]Click [ ] or press X to select · A selects all loaded tables[/]"
        self.query_one("#browser-selection-count", Static).update(text)

    def _update_action_state(self) -> None:
        """Enable/disable DESCRIBE and DROP based on cursor and checked rows."""
        has_cursor = bool(self._full_table())
        has_checked = bool(self._checked_full_tables())
        self.query_one("#describe", Button).disabled = not has_cursor
        self.query_one("#drop", Button).disabled = not has_checked
        self.query_one("#select-all", Button).disabled = not self._tables
        self._update_selection_status()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "show":
            await self.action_show_tables()
        elif event.button.id == "select-all":
            self.action_select_all()
        elif event.button.id == "describe":
            await self.action_describe()
        elif event.button.id == "drop":
            self.action_drop()
        elif event.button.id == "back":
            self.app.pop_screen()

    @staticmethod
    def _table_short_name(table_name: str) -> str:
        return table_name.rsplit(".", 1)[-1]

    def _rebuild_table_rows(self) -> None:
        """Rebuild the backing row model for the current ``self._tables``.

        Sizes start as pending placeholders; the background size worker fills
        them in one query at a time (see ``_load_table_sizes``).
        """
        self._table_rows = []
        for name in self._tables:
            self._table_rows.append(
                {
                    "name": name,
                    "type": "table",
                    "size_display": SIZE_PENDING,
                    "size_bytes": None,
                }
            )

    def _remove_dropped_tables_from_list(self, dropped_full_names: list[str]) -> None:
        """Remove dropped tables from the visible list without waiting on Impala."""
        if not dropped_full_names:
            return
        dropped_names = {
            name
            for full_name in dropped_full_names
            for name in (full_name, self._table_short_name(full_name))
        }
        self._tables = [name for name in self._tables if name not in dropped_names]
        self._table_rows = [row for row in self._table_rows if row.get("name") not in dropped_names]
        self._render_table_list()
        self._update_action_state()

    async def action_show_tables(self, *, describe_selection: bool = True) -> None:
        selected_before = self._selected_table()
        self._show_table_list_message("Loading tables…", severity="dim")
        try:
            schema = self._schema()
            filter_val = self.query_one("#filter", Input).value.strip() or "*"
            self._tables = await impala.show_tables(schema, filter_val)
        except (
            capacity.CapacityBusy,
            capacity.CapacityTimeout,
            capacity.CapacityLedgerError,
        ) as exc:
            self._show_capacity_error("SHOW TABLES", exc)
            return
        except Exception as exc:
            self._show_table_list_message(str(exc), severity="error")
            self.notify(f"SHOW TABLES failed: {exc}", severity="error")
            return

        self._checked.intersection_update(self._tables)
        self._rebuild_table_rows()

        self._render_table_list(
            selected_before=self._tables[0]
            if describe_selection and self._tables
            else selected_before
        )
        if not self._tables:
            self._checked.clear()
            self._show_detail_placeholder()
        elif describe_selection:
            await self.action_describe()
        self._update_action_state()
        self._start_size_fetch()

    def _start_size_fetch(self) -> None:
        """Fill the Size column in the background without blocking the list.

        Shared capacity admits at most two stats commands. ``exclusive=True``
        cancels any fetch still running from a previous load.
        """
        self._size_worker_generation += 1
        generation = self._size_worker_generation
        if not self._tables:
            self._sizes_loading = False
            return
        names = [str(row["name"]) for row in self._sorted_rows()]
        self._sizes_loading = True
        self._update_sort_indicator()
        # Exclusivity is scoped by group; keep this worker out of the default
        # group so it never cancels (or is cancelled by) the drop flow.
        self.run_worker(
            self._load_table_sizes(names, generation),
            name="table-sizes",
            group="table-sizes",
            exclusive=True,
        )

    async def _load_table_sizes(self, names: list[str], generation: int) -> None:
        """Fetch sizes in shared-capacity batches, updating cells in place."""
        rows_by_name = {str(row["name"]): row for row in self._table_rows}
        try:
            async for name, stats in impala.iter_table_sizes(self._schema(), names):
                row = rows_by_name.get(name)
                if row is None or row not in self._table_rows:
                    # The table was dropped or the list rebuilt while this fetch
                    # was in flight; skip the stale result.
                    continue
                row["size_display"] = stats.size_display
                row["size_bytes"] = stats.size_bytes
                self._update_size_cell(name, stats.size_display)
        except capacity.CapacityLedgerError as exc:
            self._show_capacity_error("SHOW TABLE STATS", exc)
        finally:
            if generation == self._size_worker_generation:
                self._sizes_loading = False
        if generation == self._size_worker_generation:
            self._update_sort_indicator()
            if self._sort_mode == "size" and self._table_rows:
                self._render_table_list(selected_before=self._selected_table())

    def _update_size_cell(self, name: str, size_display: str) -> None:
        table = self.query_one("#browser-table", DataTable)
        if self._size_column_key is None:
            return
        try:
            # Keep the fixed Size width from _sync_column_widths; auto-growing
            # here reintroduces horizontal overflow on narrow SSH terminals.
            table.update_cell(name, self._size_column_key, size_display, update_width=False)
        except CellDoesNotExist:
            pass

    def _sorted_rows(self) -> list[_TableRow]:
        rows = list(self._table_rows)
        rows.sort(key=self._sort_key, reverse=self._sort_reverse)
        return rows

    def _update_sort_indicator(self) -> None:
        # Size mode lists the largest table first when not reversed, so the
        # arrow must reflect the direction actually shown, not _sort_reverse.
        ascending = self._sort_reverse if self._sort_mode == "size" else not self._sort_reverse
        arrow = "\u2191" if ascending else "\u2193"
        suffix = " \u00b7 sizes loading\u2026" if self._sizes_loading else ""
        self.query_one("#browser-sort-indicator", Static).update(
            f"[dim]Sorted by: {self._sort_mode} {arrow}{suffix}[/]"
        )

    def _render_table_list(self, *, selected_before: str = "") -> None:
        rows = self._sorted_rows()

        table = self.query_one("#browser-table", DataTable)
        table.clear()

        self.query_one("#browser-count", Static).update(f"[dim]{len(self._tables)} tables[/]")
        self._update_sort_indicator()

        if not self._tables:
            table.add_row(UNCHECKED_MARKER, NO_TABLES_PLACEHOLDER, SIZE_UNKNOWN, "")
            table.cursor_coordinate = (0, 0)
            self._sync_column_widths()
            return

        selected_row = 0
        for index, row in enumerate(rows):
            name = str(row["name"])
            table.add_row(
                self._check_marker(name),
                name,
                row["size_display"],
                row["type"],
                key=name,
            )
            if selected_before and name == selected_before:
                selected_row = index
        table.cursor_coordinate = (selected_row, 0)
        # Recompute after rows exist so a newly-shown vertical scrollbar is
        # reserved and columns stay within the visible width.
        self._sync_column_widths()

    def _sort_key(self, row: _TableRow) -> tuple[object, ...]:
        if self._sort_mode == "size":
            size_bytes = row["size_bytes"]
            return (size_bytes is None, -(size_bytes or 0), row["name"].lower())
        return (row["name"].lower(),)

    def action_cycle_sort(self) -> None:
        idx = self.SORT_MODES.index(self._sort_mode)
        next_idx = (idx + 1) % len(self.SORT_MODES)
        if next_idx == 0:
            self._sort_reverse = not self._sort_reverse
        self._sort_mode = self.SORT_MODES[next_idx]
        if self._table_rows:
            self._render_table_list(selected_before=self._selected_table())
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
        except (
            capacity.CapacityBusy,
            capacity.CapacityTimeout,
            capacity.CapacityLedgerError,
        ) as exc:
            self._show_capacity_error("DESCRIBE", exc)
            result = str(exc)
        except Exception as exc:
            result = str(exc)

        self._describe_text = result
        self.query_one("#file-preview-title", Static).update(f"[bold cyan]{full}[/]")
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

        self.query_one("#browser-selected", Static).update(f"[cyan]Selected: {full}[/]")
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
                columns.append(
                    {
                        "name": parts[0],
                        "type": parts[1],
                        "comment": parts[2] if len(parts) > 2 else "",
                    }
                )
        return columns

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Clicking the Size header sorts by size; repeated clicks toggle direction."""
        if event.data_table.id != "browser-table":
            return
        if self._size_column_key is None or event.column_key != self._size_column_key:
            return
        if self._sort_mode == "size":
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_mode = "size"
            # Default size sort is largest-first (see _sort_key / indicator).
            self._sort_reverse = False
        if self._table_rows:
            self._render_table_list(selected_before=self._selected_table())
            self._update_action_state()

    def on_browser_table_sel_clicked(self, event: BrowserTable.SelClicked) -> None:
        """Toggle drop-selection when the Sel checkbox cell is clicked."""
        if event.data_table.id != "browser-table":
            return
        name = str(event.row_key.value)
        self._toggle_check_named(name)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update button state whenever the cursor moves to a different row."""
        self._update_action_state()

    def action_cursor_down(self) -> None:
        if self.query_one("#browser-table", DataTable).has_focus:
            self.query_one("#browser-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.query_one("#browser-table", DataTable).has_focus:
            self.query_one("#browser-table", DataTable).action_cursor_up()

    def _toggle_check_named(self, name: str) -> None:
        if not name or name == NO_TABLES_PLACEHOLDER or name not in self._tables:
            return
        if name in self._checked:
            self._checked.remove(name)
        else:
            self._checked.add(name)
        self._render_table_list(selected_before=name)
        self._update_action_state()

    def action_toggle_check(self) -> None:
        """Toggle drop-selection for the highlighted table."""
        self._toggle_check_named(self._selected_table())

    def action_select_all(self) -> None:
        if not self._tables:
            return
        if len(self._checked) == len(self._tables):
            self._checked.clear()
        else:
            self._checked = set(self._tables)
        cursor_name = self._selected_table()
        self._render_table_list(selected_before=cursor_name if cursor_name in self._tables else "")
        self._update_action_state()

    def action_drop(self) -> Worker[None]:
        """Run the confirm-and-drop flow in a worker (see NewJobScreen.action_launch)."""
        return self.run_worker(
            self._drop_flow(), name="drop-flow", group="drop-flow", exclusive=True
        )

    async def _drop_tables(self, tables: list[str]) -> tuple[list[str], list[str]]:
        short_names_by_full = {self._qualify_table(name): name for name in self._tables}
        dropped: list[str] = []
        errors: list[str] = []
        for full_table in tables:
            try:
                await impala.drop_table(full_table)
                dropped.append(full_table)
                short_name = short_names_by_full.get(full_table)
                if short_name:
                    self._checked.discard(short_name)
            except (
                capacity.CapacityBusy,
                capacity.CapacityTimeout,
                capacity.CapacityLedgerError,
            ) as exc:
                self._show_capacity_error("DROP", exc)
                errors.append(f"{full_table}: {exc}")
            except Exception as exc:
                errors.append(f"{full_table}: {exc}")
        return dropped, errors

    def _notify_drop_results(self, dropped: list[str], errors: list[str]) -> None:
        if dropped:
            self.notify(
                f"Dropped {len(dropped)} table(s): {', '.join(dropped)}",
                severity="information",
            )
        if errors:
            self.notify(f"DROP failed for {len(errors)} table(s).", severity="error")

    def _show_drop_results(self, dropped: list[str], errors: list[str]) -> None:
        if errors and not dropped:
            self._show_detail_message("\n".join(errors), severity="error")
        elif errors:
            summary = "Dropped:\n" + "\n".join(f"  • {name}" for name in dropped)
            summary += "\n\nFailed:\n" + "\n".join(f"  • {message}" for message in errors)
            self._show_detail_message(summary, severity="error")
        elif dropped:
            summary = "Dropped:\n" + "\n".join(f"  • {name}" for name in dropped)
            self._show_detail_message(summary, severity="success")

    async def _drop_flow(self) -> None:
        tables = self._checked_full_tables()
        if not tables:
            self.notify("Select one or more tables to drop.", severity="warning")
            return
        if not await self._confirm_drop(tables):
            return

        dropped, errors = await self._drop_tables(tables)
        self._notify_drop_results(dropped, errors)

        # Remove successful drops immediately, then confirm the list with Impala.
        if dropped:
            await self._refresh_after_successful_drop(dropped)
        else:
            await self.action_show_tables(describe_selection=False)

        self._show_drop_results(dropped, errors)

    async def _refresh_after_successful_drop(self, dropped_full_names: list[str]) -> None:
        """Refresh the Browse table list after DROP succeeds."""
        self._remove_dropped_tables_from_list(dropped_full_names)
        await self.action_show_tables(describe_selection=False)

    async def _confirm_drop(self, full_tables: list[str]) -> bool:
        table_lines = "\n".join(f"  • [cyan]{name}[/]" for name in full_tables)
        count = len(full_tables)
        title = "DROP TABLE" if count == 1 else f"DROP {count} TABLES"
        body = (
            f"Drop the following table{'s' if count != 1 else ''}?\n\n"
            f"{table_lines}\n\n"
            "[red]This cannot be undone.[/]\n"
            "Type I AM SURE, then DROP to confirm."
        )
        result = await self.app.push_screen_wait(
            ConfirmScreen(
                title,
                body,
                danger=True,
                confirm_label="Drop",
                cancel_label="Keep Table" if count == 1 else "Keep Tables",
                required_confirmation_text="I AM SURE",
                secondary_confirmation_text="DROP",
            )
        )
        return bool(result)

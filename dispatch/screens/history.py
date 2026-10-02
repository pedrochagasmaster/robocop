"""History screen for Jobs older than the dashboard window."""

from __future__ import annotations

import asyncio
from typing import Protocol, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from .. import jobs
from ..formatting import format_job_id, format_state, format_timestamp
from .sidebar import Sidebar

PAGE_SIZE = 17


class _JobDetailNavigator(Protocol):
    def open_job_detail(self, job_id: str, *, cancel_on_mount: bool = False) -> None: ...


class HistoryScreen(Screen[None]):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("enter", "view_logs", "View Logs"),
        ("[", "prev_page", "Prev Page"),
        ("]", "next_page", "Next Page"),
        ("s", "cycle_sort", "Sort"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    SORT_MODES = ("date", "state", "table")

    def __init__(self) -> None:
        super().__init__()
        self._page = 0
        self._all_jobs: list[dict] = []
        self._filtered: list[dict] = []
        self._sort_mode = "date"
        self._sort_reverse = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        sidebar = Sidebar()
        sidebar.active_screen = "history"
        yield sidebar
        with Vertical(id="main-content"):
            with Vertical(id="history-content"):
                yield Static(
                    "[bold]Job History[/] [dim]\u00b7 finished more than 7 days ago[/]",
                    classes="section-title",
                )
                yield Static("[dim]Sorted by: date \u2193[/]", id="sort-indicator")

                with Horizontal(id="search-row"):
                    yield Static("[dim]Search:[/]", id="search-label")
                    yield Input(
                        placeholder="Filter: table, date, or job-id",
                        id="search",
                    )

                yield DataTable(id="history-table")
                with Vertical(id="history-empty", classes="empty-state"):
                    yield Static(
                        "[dim]No history found \u2014 adjust your search, or check Overview for recent jobs[/]"
                    )

                with Horizontal(id="pagination"):
                    yield Static("", id="page-info")
                    yield Static("", id="page-controls")

            with Horizontal(classes="action-bar"):
                yield Static("", id="history-status", classes="action-status")
                yield Button("Back [B]", id="back", variant="default")
                yield Button("View Logs [Enter]", id="view-logs", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.add_columns("ID", "Table/Target", "State", "Finished At")
        table.cursor_type = "row"
        self.query_one("#history-empty").display = False
        self.query_one("#history-status", Static).update("Loading history\u2026")
        # Paint the shell before touching older manifests on the Edge filesystem.
        self.run_worker(
            self._load_history_async(),
            name="history-initial-load",
            group="history-load",
            exclusive=True,
        )
        self.query_one("#search", Input).focus()

    async def _load_history_async(self) -> None:
        items = await asyncio.to_thread(jobs.history_jobs)
        if not self.is_mounted or self.app.screen is not self:
            return
        self._all_jobs = items
        self._filtered = items
        self._render_history()
        if self._filtered:
            self.query_one("#history-table", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self._page = 0
            # Search/sort/pagination are pure view changes over the cached
            # manifest list; no filesystem walk per keystroke.
            self._render_history()

    def refresh_history(self) -> None:
        """Reload manifests from disk, then re-render the table."""
        self._all_jobs = jobs.history_jobs()
        self._render_history()

    def _render_history(self) -> None:
        needle = self.query_one("#search", Input).value.lower().strip()
        self._filtered = []
        for item in self._all_jobs:
            dest = item["destination"]
            table_name = f"{dest.get('schema', '')}.{dest.get('table_name', '')}"
            haystack = f"{item['id']} {table_name} {item['finished_at']}".lower()
            if needle and needle not in haystack:
                continue
            self._filtered.append(item)

        self._filtered.sort(key=self._sort_key, reverse=self._sort_reverse)

        total = len(self._filtered)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self._page = min(self._page, total_pages - 1)
        start = self._page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_items = self._filtered[start:end]

        table = self.query_one("#history-table", DataTable)
        table.clear()
        for item in page_items:
            dest = item["destination"]
            table_name = dest.get("table_name") or dest.get("csv_path", "")
            if "/" in table_name:
                table_name = table_name.split("/")[-1]

            table.add_row(
                self._display_id(item["id"]),
                table_name[:25],
                format_state(item["state"]),
                format_timestamp(item["finished_at"]),
                key=item["id"],
            )

        if not self._filtered:
            table.display = False
            self.query_one("#history-empty").display = True
            self.query_one("#pagination").display = False
            if table.has_focus:
                self.query_one("#search", Input).focus()
        else:
            table.display = True
            self.query_one("#history-empty").display = False
            self.query_one("#pagination").display = True

        self.query_one("#page-info", Static).update(
            f"[dim]Showing {start + 1}-{min(end, total)} of {total}[/]"
            if total > 0
            else "[dim]No results[/]"
        )
        self.query_one("#history-status", Static).update(
            f"{total} finished job{'s' if total != 1 else ''}"
            + (f" matching \u201c{needle}\u201d" if needle else "")
        )
        self.query_one("#page-controls", Static).update(
            f"[dim]\u276e Prev    Page {self._page + 1} of {total_pages}    Next \u276f[/]"
        )
        arrow = "\u2193" if self._sort_reverse else "\u2191"
        self.query_one("#sort-indicator", Static).update(
            f"[dim]Sorted by: {self._sort_mode} {arrow}[/]"
        )

    def _sort_key(self, item: dict) -> str:
        if self._sort_mode == "state":
            return item.get("state", "")
        if self._sort_mode == "table":
            dest = item.get("destination", {})
            return f"{dest.get('schema', '')}.{dest.get('table_name', '')}"
        return item.get("finished_at") or item.get("id", "")

    def action_cycle_sort(self) -> None:
        idx = self.SORT_MODES.index(self._sort_mode)
        next_idx = (idx + 1) % len(self.SORT_MODES)
        if next_idx == 0:
            self._sort_reverse = not self._sort_reverse
        self._sort_mode = self.SORT_MODES[next_idx]
        self._page = 0
        self._render_history()

    def action_cursor_down(self) -> None:
        table = self.query_one("#history-table", DataTable)
        if table.has_focus:
            table.action_cursor_down()

    def action_cursor_up(self) -> None:
        table = self.query_one("#history-table", DataTable)
        if table.has_focus:
            table.action_cursor_up()

    def action_next_page(self) -> None:
        total_pages = max(1, (len(self._filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
        if self._page < total_pages - 1:
            self._page += 1
            self._render_history()

    def action_prev_page(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._render_history()

    @staticmethod
    def _display_id(job_id: str) -> str:
        return format_job_id(job_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_key = str(event.row_key.value) if event.row_key else ""
        if row_key and row_key != "__empty__":
            cast(_JobDetailNavigator, self.app).open_job_detail(row_key)

    def _selected_job_id(self) -> str | None:
        table = self.query_one("#history-table", DataTable)
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            row_key = str(cell_key.row_key.value)
        except Exception:
            return None
        if row_key and row_key != "__empty__":
            return row_key
        return None

    def action_view_logs(self) -> None:
        row_key = self._selected_job_id()
        if row_key:
            cast(_JobDetailNavigator, self.app).open_job_detail(row_key)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "view-logs":
            self.action_view_logs()
        elif event.button.id == "back":
            self.app.pop_screen()

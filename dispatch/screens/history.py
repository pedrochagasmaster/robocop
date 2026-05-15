"""History screen for Jobs older than the dashboard window."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from .. import jobs
from .job_detail import JobDetailScreen
from .sidebar import Sidebar

PAGE_SIZE = 17


class HistoryScreen(Screen[None]):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("enter", "view_logs", "View Logs"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._page = 0
        self._filtered: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        sidebar = Sidebar()
        sidebar.active_screen = "history"
        yield sidebar
        with Vertical(id="main-content"):
            with Vertical(id="history-content"):
                yield Static("History", classes="section-title")

                with Horizontal(id="search-row"):
                    yield Input(
                        placeholder="\U0001f50d Search by table/date/job id",
                        id="search",
                    )
                    yield Input(placeholder="\u2192 Job id to view", id="job-id")

                yield DataTable(id="history-table")

                with Horizontal(id="pagination"):
                    yield Static("", id="page-info")
                    yield Static("", id="page-controls")

                with Horizontal(classes="button-row"):
                    yield Button("View Logs [Enter]", id="view-logs", variant="primary")
                    yield Button("Back [B]", id="back", variant="default")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.add_columns("ID", "Table", "State", "Finished At \u2193")
        table.cursor_type = "row"
        self.refresh_history()

    def on_input_changed(self, _event: Input.Changed) -> None:
        self._page = 0
        self.refresh_history()

    def refresh_history(self) -> None:
        needle = self.query_one("#search", Input).value.lower().strip()
        self._filtered = []
        for item in jobs.history_jobs():
            dest = item["destination"]
            table_name = f"{dest.get('schema', '')}.{dest.get('table_name', '')}"
            haystack = f"{item['id']} {table_name} {item['finished_at']}".lower()
            if needle and needle not in haystack:
                continue
            self._filtered.append(item)

        total = len(self._filtered)
        start = self._page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_items = self._filtered[start:end]

        table = self.query_one("#history-table", DataTable)
        table.clear()
        for item in page_items:
            dest = item["destination"]
            table_name = f"{dest.get('schema', '')}.{dest.get('table_name', '')}"
            state = item["state"]
            if state == "Succeeded":
                state_display = "[green]\u25cf SUCCEEDED[/]"
            elif state == "Failed":
                state_display = "[red]\u25cf FAILED[/]"
            elif state == "Cancelled":
                state_display = "[dim]\u25cf CANCELLED[/]"
            else:
                state_display = f"\u25cf {state}"
            table.add_row(
                item["id"][:24],
                table_name[:25],
                state_display,
                item["finished_at"] or "",
            )
        if not page_items:
            table.add_row("(no history)", "", "", "")

        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.query_one("#page-info", Static).update(
            f"[dim]Showing {start + 1}-{min(end, total)} of {total}[/]"
            if total > 0
            else "[dim]No results[/]"
        )
        self.query_one("#page-controls", Static).update(
            f"[dim]\u276e Prev    Page {self._page + 1} of {total_pages}    Next \u276f[/]"
        )

    def action_view_logs(self) -> None:
        job_id = self.query_one("#job-id", Input).value.strip()
        if job_id:
            self.app.push_screen(JobDetailScreen(job_id))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "view-logs":
            self.action_view_logs()
        elif event.button.id == "back":
            self.app.pop_screen()

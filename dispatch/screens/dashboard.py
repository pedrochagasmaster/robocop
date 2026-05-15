"""Dashboard screen for active and recently finished Jobs."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from .. import jobs, kerberos
from .browser import BrowserScreen
from .history import HistoryScreen
from .job_detail import JobDetailScreen
from .new_job import NewJobScreen
from .sidebar import NavItem, Sidebar


class DashboardScreen(Screen[None]):
    BINDINGS = [
        ("n", "new_job", "New Job"),
        ("v", "view_logs", "View Logs"),
        ("a", "view_logs", "Attach"),
        ("c", "cancel", "Cancel"),
        ("h", "history", "History"),
        ("b", "browser", "Browse"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, launch_cwd: Path) -> None:
        super().__init__()
        self.launch_cwd = launch_cwd
        self.kerberos_ttl: int | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        sidebar = Sidebar()
        sidebar.active_screen = "overview"
        yield sidebar
        with Vertical(id="main-content"):
            with Vertical(id="dashboard-content"):
                with Horizontal(id="stats-row"):
                    with Vertical(classes="stat-card"):
                        yield Static("RUNNING", classes="stat-label")
                        yield Static("0 / 2", id="stat-running", classes="stat-value stat-green")
                        yield Static("active / limit", classes="stat-sub")
                    with Vertical(classes="stat-card"):
                        yield Static("FINISHED (7D)", classes="stat-label")
                        yield Static("0", id="stat-finished", classes="stat-value stat-green")
                        yield Static("last 7 days", classes="stat-sub")
                    with Vertical(classes="stat-card"):
                        yield Static("FAILED (7D)", classes="stat-label")
                        yield Static("0", id="stat-failed", classes="stat-value stat-red")
                        yield Static("last 7 days", classes="stat-sub")
                    with Vertical(classes="stat-card"):
                        yield Static("KERBEROS", classes="stat-label")
                        yield Static("--", id="stat-kerberos", classes="stat-value stat-yellow")
                        yield Static("remaining", classes="stat-sub")
                yield Static("Active Jobs", classes="section-title")
                yield DataTable(id="active-table")
                yield Static("Recently Finished (last 7 days)", classes="section-title")
                yield DataTable(id="recent-table")
                with Vertical(id="job-id-section"):
                    yield Static("Job ID (attach/cancel)")
                    yield Input(placeholder="Enter job ID...", id="job-id")
                with Horizontal(classes="button-row"):
                    yield Button("New Job [N]", id="new-job", variant="primary")
                    yield Button("View Logs [V]", id="view-logs", variant="default")
                    yield Button("Cancel [C]", id="cancel", variant="error")
        yield Footer()

    async def on_mount(self) -> None:
        active_table = self.query_one("#active-table", DataTable)
        active_table.add_columns("ID", "Source", "Destination", "State", "Started")
        active_table.cursor_type = "row"
        recent_table = self.query_one("#recent-table", DataTable)
        recent_table.add_columns("ID", "Source", "Destination", "State", "Started")
        recent_table.cursor_type = "row"
        self.refresh_jobs()
        self.set_interval(2.0, self.refresh_jobs)
        self.kerberos_ttl = await kerberos.ticket_ttl_seconds()
        self._refresh_kerberos()

    def _refresh_kerberos(self) -> None:
        label = self.query_one("#stat-kerberos", Static)
        if self.kerberos_ttl is None:
            label.update("N/A")
        else:
            minutes = self.kerberos_ttl // 60
            label.update(f"{minutes}m")

    def refresh_jobs(self) -> None:
        active = jobs.active_jobs()
        running = [j for j in active if j["state"] == "Running"]
        recent = [j for j in active if j["state"] != "Running"]

        self.query_one("#stat-running", Static).update(f"{len(running)} / 2")
        finished_count = sum(1 for j in active if j["state"] == "Succeeded")
        failed_count = sum(1 for j in active if j["state"] == "Failed")
        self.query_one("#stat-finished", Static).update(str(finished_count))
        self.query_one("#stat-failed", Static).update(str(failed_count))

        active_table = self.query_one("#active-table", DataTable)
        active_table.clear()
        for item in running:
            active_table.add_row(
                item["id"][:24],
                self._source_label(item),
                self._dest_label(item),
                "[green]\u25cf RUNNING[/]",
                (item.get("started_at") or "")[-8:],
            )
        if not running:
            active_table.add_row("(none)", "", "", "", "")

        recent_table = self.query_one("#recent-table", DataTable)
        recent_table.clear()
        for item in recent[:8]:
            state = item["state"]
            if state == "Succeeded":
                state_display = "[green]\u25cf SUCCEEDED[/]"
            elif state == "Failed":
                state_display = "[red]\u25cf FAILED[/]"
            elif state == "Cancelled":
                state_display = "[dim]\u25cf CANCELLED[/]"
            else:
                state_display = f"\u25cf {state}"
            recent_table.add_row(
                item["id"][:24],
                self._source_label(item),
                self._dest_label(item),
                state_display,
                (item.get("started_at") or "")[-8:],
            )
        if not recent:
            recent_table.add_row("(none)", "", "", "", "")

    def _source_label(self, item: dict) -> str:
        src = item.get("source", {})
        if src.get("table_name"):
            return src["table_name"][:25]
        if src.get("sql_path_at_launch"):
            return Path(src["sql_path_at_launch"]).name[:25]
        return src.get("type", "")[:25]

    def _dest_label(self, item: dict) -> str:
        dest = item.get("destination", {})
        schema = dest.get("schema", "")
        table = dest.get("table_name", "")
        if schema and table:
            return f"{schema}.{table}"[:30]
        return dest.get("type", "")[:30]

    def _selected_job_id(self) -> str | None:
        value = self.query_one("#job-id", Input).value.strip()
        if value:
            return value
        active = jobs.active_jobs()
        return active[0]["id"] if active else None

    def action_new_job(self) -> None:
        self.app.push_screen(NewJobScreen(self.launch_cwd))

    def action_view_logs(self) -> None:
        job_id = self._selected_job_id()
        if job_id:
            self.app.push_screen(JobDetailScreen(job_id))

    def action_cancel(self) -> None:
        job_id = self._selected_job_id()
        if job_id:
            self.app.push_screen(JobDetailScreen(job_id, cancel_on_mount=True))

    def action_history(self) -> None:
        self.app.push_screen(HistoryScreen())

    def action_browser(self) -> None:
        self.app.push_screen(BrowserScreen())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-job":
            self.action_new_job()
        elif event.button.id == "view-logs":
            self.action_view_logs()
        elif event.button.id == "cancel":
            self.action_cancel()

    def on_nav_item_selected(self, event: NavItem.Selected) -> None:
        if event.item_id == "new_job":
            self.app.push_screen(NewJobScreen(self.launch_cwd))
        elif event.item_id == "view_logs":
            result = self._nav_to_logs()
            if result is not None:
                self.app.push_screen(result)
        elif event.item_id == "history":
            self.app.push_screen(HistoryScreen())
        elif event.item_id == "browse":
            self.app.push_screen(BrowserScreen())

    def _nav_to_logs(self) -> JobDetailScreen | None:
        job_id = self._selected_job_id()
        return JobDetailScreen(job_id) if job_id else None

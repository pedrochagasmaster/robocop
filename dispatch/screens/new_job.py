"""New Job wizard screen."""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from .. import config, jobs, kerberos, manifest, process, sql
from .preview import PreviewScreen
from .sidebar import NavItem, Sidebar


class NewJobScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("e", "edit_sql", "Edit SQL"),
        ("k", "kinit", "kinit"),
        ("l", "launch", "Launch"),
        ("p", "preview", "Preview SQL"),
    ]

    def __init__(self, launch_cwd: Path) -> None:
        super().__init__()
        self.launch_cwd = launch_cwd
        self.source_type = "SqlFile"
        self.destination_type = "Csv"
        self.kerberos_ttl: int | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        sidebar = Sidebar()
        sidebar.active_screen = "new_job"
        yield sidebar
        with Vertical(id="main-content"):
            yield Static("", id="kerberos-status")
            with Vertical(id="new-job-content"):
                yield Static("New Job", classes="section-title")

                with Vertical(id="matrix-panel"):
                    yield Static("Source \u00d7 Destination legal cells", classes="section-title")
                    yield DataTable(id="matrix-table")

                with Vertical(id="info-panel"):
                    yield Static(
                        "[bold cyan]\u24d8[/] Auto-detected source type: [cyan]SqlFile \u2192 Table[/]",
                        id="info-detected",
                    )
                    yield Static(
                        "[yellow]\u26a0[/] Ensure the combination above matches your "
                        "source and destination selection.",
                        classes="warn-text",
                    )

                with Vertical(id="form-grid"):
                    yield Static("SQL File", classes="field-label")
                    yield Static("Existing Table", classes="field-label")
                    yield Input(value=self._default_sql_file(), placeholder="SQL File", id="sql-file")
                    yield Input(
                        value="", placeholder="e.g. analytics.events_existing", id="existing-table"
                    )

                    yield Static("Source", classes="field-label")
                    yield Static("Email (notifications)", classes="field-label")
                    yield Input(value=self.source_type, placeholder="Source type", id="source")
                    yield Input(value="", placeholder="dataops@company.com", id="email")

                    yield Static("Destination", classes="field-label")
                    yield Static("Subject (email)", classes="field-label")
                    yield Input(value=self.destination_type, placeholder="Destination type", id="destination")
                    yield Input(value="Dispatch Job", placeholder="Subject line", id="subject")

                    yield Static("Schema", classes="field-label")
                    yield Static("Start Date (YYYY-MM-DD)", classes="field-label")
                    yield Input(value="dw_settle", placeholder="Schema", id="schema")
                    yield Input(value="2026-01-01", placeholder="YYYY-MM-DD", id="start-date")

                    yield Static("Table Name", classes="field-label")
                    yield Static("End Date (YYYY-MM-DD)", classes="field-label")
                    yield Input(value="dispatch_result", placeholder="Table name", id="table-name")
                    yield Input(value="2026-01-31", placeholder="YYYY-MM-DD", id="end-date")

                yield Static("", id="warning-text")

                with Horizontal(classes="button-row"):
                    yield Button("Preview SQL [P]", id="preview", variant="primary")
                    yield Button("Launch [L]", id="launch", variant="success")

                yield Static(
                    "[dim]\u24d8 Use Preview SQL to validate the generated statement before launching.\n"
                    "  Job will be submitted to Impala over SSH.[/]",
                    id="launch-info",
                )
        yield Footer()

    async def on_mount(self) -> None:
        matrix = self.query_one("#matrix-table", DataTable)
        matrix.add_columns("SOURCE \\ DEST", "TABLE", "CSV", "TABLE+CSV")
        matrix.add_row("SqlFile", "[green]\u2713[/]", "[green]\u2713[/]", "[green]\u2713[/]")
        matrix.add_row("SqlTemplate", "[green]\u2713[/]", "[dim]\u2014[/]", "[dim]\u2014[/]")
        matrix.add_row("ExistingTable", "[dim]\u2014[/]", "[green]\u2713[/]", "[dim]\u2014[/]")
        matrix.show_cursor = False

        self.kerberos_ttl = await kerberos.ticket_ttl_seconds()
        self._refresh_kerberos()
        self._detect_sql()

    def _default_sql_file(self) -> str:
        matches = sorted(self.launch_cwd.glob("*.sql"))
        return str(matches[0]) if matches else str(self.launch_cwd / "query.sql")

    def _input_value(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()

    def _refresh_kerberos(self) -> None:
        label = self.query_one("#kerberos-status", Static)
        if self.kerberos_ttl is None:
            label.update("[yellow]Kerberos ticket missing \u2014 press K to kinit[/]")
        else:
            minutes = self.kerberos_ttl // 60
            label.update(f"[dim]Kerberos: {minutes}m remaining[/]")

    def _read_sql(self) -> str:
        sql_path = Path(self._input_value("sql-file"))
        return sql_path.read_text(encoding="utf-8")

    def _detect_sql(self) -> None:
        try:
            detected = sql.detect_source(self._read_sql())
        except OSError:
            return
        info = self.query_one("#info-detected", Static)
        info.update(f"[bold cyan]\u24d8[/] Auto-detected source type: [cyan]{detected}[/]")
        self.query_one("#source", Input).value = detected

    def _validate(self) -> str | None:
        source_type = self._input_value("source")
        destination_type = self._input_value("destination")
        if (source_type, destination_type) not in manifest.LEGAL_CELLS:
            return f"Illegal Source/Destination cell: {source_type}/{destination_type}"
        if not jobs.can_launch():
            return f"Already at the {jobs.RUNNING_CAP}-Job concurrency cap; wait for one to finish"
        if self.kerberos_ttl is None:
            return "Kerberos ticket missing"
        if self.kerberos_ttl < 300:
            return "Kerberos ticket TTL is under 5 minutes"
        sql_text = self._read_sql()
        if sql.is_malformed_template(sql_text):
            return "SQL contains only one of {{date_inicio}}/{{date_fim}} \u2014 likely a typo"
        if source_type == "SqlTemplate" and not sql.template_is_complete(sql_text):
            return "SqlTemplate requires both {{date_inicio}} and {{date_fim}}"
        return None

    def _source_destination(self) -> tuple[manifest.Source, manifest.Destination]:
        source_type = self._input_value("source")
        destination_type = self._input_value("destination")
        schema = self._input_value("schema")
        table = self._input_value("table-name")
        if source_type == "ExistingTable":
            existing = self._input_value("existing-table") or f"{schema}.{table}"
            source: manifest.Source = {"type": "ExistingTable", "table_name": existing}
            if "." in existing:
                schema, table = existing.split(".", 1)
        else:
            source = {"type": source_type, "sql_path_at_launch": self._input_value("sql-file")}
        csv_path = str(self.launch_cwd / f"{table}.csv")
        destination: manifest.Destination = {
            "type": destination_type,
            "schema": schema,
            "table_name": table,
            "csv_path": csv_path,
        }
        return source, destination

    def _params(self) -> dict[str, str]:
        params = {"to_email": self._input_value("email"), "subject": self._input_value("subject")}
        if self._input_value("source") == "SqlTemplate":
            params["start_date"] = sql.to_orchestrator_date(self._input_value("start-date"))
            params["end_date"] = sql.to_orchestrator_date(self._input_value("end-date"))
        return params

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "launch":
            await self.action_launch()
        elif event.button.id == "preview":
            self.action_preview()

    def action_preview(self) -> None:
        source_type = self._input_value("source")
        schema = self._input_value("schema")
        table = self._input_value("table-name")
        try:
            sql_text = self._read_sql()
        except OSError as exc:
            self.query_one("#warning-text", Static).update(f"[red]{exc}[/]")
            return
        if source_type == "SqlTemplate":
            preview = sql.monthly_preview(
                sql_text, schema, table,
                self._input_value("start-date"),
                self._input_value("end-date"),
            )
        else:
            preview = sql.table_wrapper(sql_text, schema, table, config.current_user())
        self.app.push_screen(
            PreviewScreen("SQL Preview", preview, schema=schema, table=table)
        )

    async def action_launch(self) -> None:
        error = self._validate()
        if error:
            self.query_one("#warning-text", Static).update(f"[red]{error}[/]")
            return
        source, destination = self._source_destination()
        job_dir, _job_manifest = manifest.create_job(
            source=source,
            destination=destination,
            params=self._params(),
            launch_cwd=self.launch_cwd,
            sql_text=self._read_sql(),
        )
        await process.launch_runner(job_dir)
        self.query_one("#warning-text", Static).update(
            f"[green]\u2713 Launched Job {job_dir.name}[/]"
        )

    def action_edit_sql(self) -> None:
        editor = os.environ.get("EDITOR", "vi")
        with self.app.suspend():
            process.run_interactive(editor, self._input_value("sql-file"))
        self._detect_sql()

    async def action_kinit(self) -> None:
        with self.app.suspend():
            process.run_interactive("kinit")
        self.kerberos_ttl = await kerberos.ticket_ttl_seconds()
        self._refresh_kerberos()

    def on_nav_item_selected(self, event: NavItem.Selected) -> None:
        if event.item_id == "overview":
            self.app.pop_screen()
        elif event.item_id != "new_job":
            self.dismiss()

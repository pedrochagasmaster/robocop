"""New Job wizard screen."""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
from datetime import date, datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    RadioButton,
    RadioSet,
    Static,
)
from textual.worker import Worker

from .. import config, jobs, manifest, process, sql, telemetry
from .confirm import ConfirmScreen
from .preview import PreviewScreen
from .sidebar import Sidebar

logger = logging.getLogger("dispatch.new_job")


def _refusal_reason(error: str) -> str:
    lowered = error.lower()
    if "concurrency cap" in lowered:
        return "slot_cap"
    if "kerberos" in lowered:
        return "kerberos"
    return "validation"

_SOURCE_IDS = {
    "src-sqlfile": "SqlFile",
    "src-sqltemplate": "SqlTemplate",
    "src-existingtable": "ExistingTable",
}
_DEST_IDS = {"dst-table": "Table", "dst-csv": "Csv", "dst-table-csv": "Table+Csv"}


class NewJobScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("e", "edit_sql", "Edit SQL"),
        ("k", "kinit", "kinit"),
        ("l", "launch", "Launch"),
        ("p", "preview", "Preview SQL"),
        ("m", "toggle_matrix", "Matrix"),
    ]

    def __init__(self, launch_cwd: Path, prefill: dict | None = None) -> None:
        super().__init__()
        self.launch_cwd = launch_cwd
        self.prefill = prefill or {}
        self.kerberos_ttl: int | None = None
        self._matrix_collapsed = bool(config.read_form_defaults())
        self._prefilled = bool(self.prefill)
        self._cwd_sql_files: list[dict] = []
        self._picker_ready = False
        # (path, exists) memo so keystrokes in unrelated fields do not stat()
        # the SQL path (potentially a slow network mount) on every change.
        self._sql_exists_cache: tuple[str, bool] | None = None
        self._validation_summary_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        sidebar = Sidebar()
        sidebar.active_screen = "new_job"
        yield sidebar
        with Vertical(id="main-content"):
            with Vertical(id="new-job-content"):
                yield Static("[bold]New Job[/]", classes="section-title")

                with Collapsible(
                    title="Source \u00d7 Destination legal cells",
                    collapsed=self._matrix_collapsed,
                    id="matrix-collapsible",
                ):
                    yield DataTable(id="matrix-table")

                yield Static(
                    "Detected source: SqlFile \u00b7 illegal destinations are disabled automatically",
                    id="info-detected",
                )

                with Vertical(id="radio-panel"):
                    with Horizontal(id="radio-row"):
                        with Vertical(classes="radio-group"):
                            yield Static("Source", classes="field-label")
                            with RadioSet(id="source"):
                                yield RadioButton("SqlFile", value=True, id="src-sqlfile")
                                yield RadioButton("SqlTemplate", id="src-sqltemplate")
                                yield RadioButton("ExistingTable", id="src-existingtable")
                        with Vertical(classes="radio-group"):
                            yield Static("Destination", classes="field-label")
                            with RadioSet(id="destination"):
                                yield RadioButton("Table", id="dst-table")
                                yield RadioButton("Csv", value=True, id="dst-csv")
                                yield RadioButton("Table+Csv", id="dst-table-csv")
                    yield Static("", id="dest-hint")

                yield Static("", id="picker-caption", classes="input-caption")
                yield DataTable(id="sql-file-picker")

                with Vertical(id="form-grid"):
                    with Horizontal(classes="form-row", id="row-sql-file"):
                        yield Static("SQL File", classes="field-label", id="lbl-sql-file")
                        yield Input(
                            value=self._default_sql_file(), placeholder="SQL File", id="sql-file"
                        )
                    yield Static("", classes="path-hint", id="path-hint")

                    with Horizontal(classes="form-row", id="row-existing-table"):
                        yield Static(
                            "Existing Table", classes="field-label", id="lbl-existing-table"
                        )
                        yield Input(
                            value="",
                            placeholder="e.g. analytics.events_existing",
                            id="existing-table",
                        )

                    with Horizontal(classes="form-row", id="row-schema"):
                        yield Static("Schema", classes="field-label", id="lbl-schema")
                        yield Input(value="aa_enc", placeholder="Schema", id="schema")

                    with Horizontal(classes="form-row", id="row-table-name"):
                        yield Static("Table Name", classes="field-label", id="lbl-table-name")
                        yield Input(
                            value="dispatch_result", placeholder="Table name", id="table-name"
                        )

                    with Horizontal(classes="form-row", id="row-start-date"):
                        yield Static("Start Date", classes="field-label", id="lbl-start-date")
                        yield Input(
                            value=self._default_start_date(),
                            placeholder="YYYY-MM-DD",
                            id="start-date",
                        )

                    with Horizontal(classes="form-row", id="row-end-date"):
                        yield Static("End Date", classes="field-label", id="lbl-end-date")
                        yield Input(
                            value=self._default_end_date(), placeholder="YYYY-MM-DD", id="end-date"
                        )

                    with Horizontal(classes="form-row", id="row-email"):
                        yield Static("Email (notifications)", classes="field-label", id="lbl-email")
                        yield Input(
                            value=os.environ.get("DISPATCH_EMAIL", ""),
                            placeholder=os.environ.get("DISPATCH_EMAIL", "user@example.com"),
                            id="email",
                        )

                    with Horizontal(classes="form-row", id="row-subject"):
                        yield Static("Subject (email)", classes="field-label", id="lbl-subject")
                        yield Input(value="Dispatch Job", placeholder="Subject line", id="subject")

                yield Static("", id="warning-text")

            with Horizontal(id="new-job-action-bar", classes="action-bar"):
                yield Static("", id="validation-summary", classes="action-status")
                yield Button("Preview SQL [P]", id="preview", variant="default")
                yield Button("Launch [L]", id="launch", variant="primary")
        yield Footer()

    async def on_mount(self) -> None:
        matrix = self.query_one("#matrix-table", DataTable)
        matrix.add_columns("SOURCE \\ DEST", "TABLE", "CSV", "TABLE+CSV")
        matrix.add_row("SqlFile", "[green]\u2713[/]", "[green]\u2713[/]", "[green]\u2713[/]")
        matrix.add_row("SqlTemplate", "[green]\u2713[/]", "[dim]\u2014[/]", "[dim]\u2014[/]")
        matrix.add_row("ExistingTable", "[dim]\u2014[/]", "[green]\u2713[/]", "[dim]\u2014[/]")
        matrix.show_cursor = False

        picker = self.query_one("#sql-file-picker", DataTable)
        picker.add_columns("File", "Detected", "Modified")
        picker.cursor_type = "row"
        picker.display = False
        self.query_one("#picker-caption").display = False

        self._apply_saved_defaults()
        self._apply_prefill()
        if hasattr(type(self.app), "kerberos_ttl"):
            self.watch(self.app, "kerberos_ttl", self._on_kerberos_change, init=True)
        self._detect_sql()
        self._update_field_visibility()
        self._inline_validate()
        self._update_validation_summary()
        # Focus the Source radio set: first interactive control, and unlike an
        # Input it does not swallow the single-key mnemonics (P/L/E/K/M).
        self.query_one("#source", RadioSet).focus()
        self.run_worker(self._populate_sql_picker(), name="sql-picker", exclusive=True)

    def _scan_cwd_sql_files(self) -> list[dict]:
        results = []
        for path in sorted(self.launch_cwd.glob("*.sql")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:8192]
                mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                continue
            results.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "detected": sql.detect_source(text),
                    "mtime": mtime,
                }
            )
        return results

    async def _populate_sql_picker(self) -> None:
        """File-first launch: list cwd SQL files so no path typing is needed."""
        self._cwd_sql_files = await asyncio.to_thread(self._scan_cwd_sql_files)
        picker = self.query_one("#sql-file-picker", DataTable)
        picker.clear()
        for entry in self._cwd_sql_files:
            detected = entry["detected"]
            detected_markup = f"[cyan]{detected}[/]" if detected == "SqlTemplate" else detected
            picker.add_row(
                entry["name"],
                detected_markup,
                f"[dim]{entry['mtime']}[/]",
                key=entry["path"],
            )
        self.query_one("#picker-caption", Static).update(
            f"[dim]SQL files in {self.launch_cwd} \u00b7 pick one to fill the form[/]"
        )
        current = self._input_value("sql-file")
        for index, entry in enumerate(self._cwd_sql_files):
            if entry["path"] == current:
                picker.move_cursor(row=index)
                break
        self._picker_ready = True
        self._update_field_visibility()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Only user-driven movement (focused picker) updates the form, so a
        # prefilled custom path is never clobbered by the initial populate.
        if (
            event.data_table.id != "sql-file-picker"
            or not self._picker_ready
            or not event.data_table.has_focus
        ):
            return
        self._apply_picker_path(str(event.row_key.value) if event.row_key else "")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "sql-file-picker" or not self._picker_ready:
            return
        self._apply_picker_path(str(event.row_key.value) if event.row_key else "")

    def _apply_picker_path(self, path: str) -> None:
        if not path:
            return
        sql_input = self.query_one("#sql-file", Input)
        if sql_input.value != path:
            sql_input.value = path
            self._detect_sql()

    @staticmethod
    def _default_start_date() -> str:
        today = date.today()
        return today.replace(day=1).isoformat()

    @staticmethod
    def _default_end_date() -> str:
        today = date.today()
        last_day = calendar.monthrange(today.year, today.month)[1]
        return today.replace(day=last_day).isoformat()

    def _default_sql_file(self) -> str:
        matches = sorted(self.launch_cwd.glob("*.sql"))
        return str(matches[0]) if matches else str(self.launch_cwd / "query.sql")

    def _input_value(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()

    def _selected_source(self) -> str:
        radio_set = self.query_one("#source", RadioSet)
        if radio_set.pressed_button is not None:
            return _SOURCE_IDS.get(radio_set.pressed_button.id or "", "SqlFile")
        return "SqlFile"

    def _selected_destination(self) -> str:
        radio_set = self.query_one("#destination", RadioSet)
        if radio_set.pressed_button is not None:
            return _DEST_IDS.get(radio_set.pressed_button.id or "", "Csv")
        return "Csv"

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._update_field_visibility()
        self._inline_validate()
        self._update_validation_summary()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._inline_validate()
        self._schedule_validation_summary()
        if event.input.id == "sql-file":
            self._refresh_path_hint()

    def on_unmount(self) -> None:
        if self._validation_summary_timer is not None:
            self._validation_summary_timer.stop()
            self._validation_summary_timer = None

    def action_toggle_matrix(self) -> None:
        collapsible = self.query_one("#matrix-collapsible", Collapsible)
        collapsible.collapsed = not collapsible.collapsed

    def _sql_file_exists(self) -> bool:
        """Memoized stat of the SQL path; one stat per distinct value."""
        raw = self._input_value("sql-file")
        cached = self._sql_exists_cache
        if cached is not None and cached[0] == raw:
            return cached[1]
        exists = Path(raw).exists() if raw else False
        self._sql_exists_cache = (raw, exists)
        return exists

    def _inline_validate(self) -> None:
        """Provide real-time field validation indicators."""
        source = self._selected_source()
        msgs = []
        if source in ("SqlFile", "SqlTemplate"):
            if self._sql_file_exists():
                msgs.append("[green]\u2713[/] SQL file found")
            elif self._input_value("sql-file"):
                msgs.append("[red]\u2717[/] SQL file not found")
        email = self._input_value("email")
        if email:
            if "@" in email and "." in email.split("@")[-1]:
                msgs.append("[green]\u2713[/] Email")
            else:
                msgs.append("[red]\u2717[/] Invalid email format")
        if self.kerberos_ttl is None:
            msgs.append("[red]\u2717[/] Kerberos missing")
        elif self.kerberos_ttl < 300:
            msgs.append("[yellow]\u26a0[/] Kerberos TTL low")
        else:
            msgs.append("[green]\u2713[/] Kerberos")
        self.query_one("#warning-text", Static).update("  ".join(msgs))

    def _validation_issues(self) -> list[str]:
        issues: list[str] = []
        source = self._selected_source()
        destination = self._selected_destination()
        if (source, destination) not in manifest.LEGAL_CELLS:
            issues.append(f"Illegal combination: {source} \u2192 {destination}")
        if destination in ("Table", "Table+Csv"):
            schema_error = sql.validate_identifier(self._input_value("schema"), "Schema")
            if schema_error:
                issues.append(schema_error)
            table_error = sql.validate_identifier(self._input_value("table-name"), "Table name")
            if table_error:
                issues.append(table_error)
        if source in ("SqlFile", "SqlTemplate"):
            if not self._input_value("sql-file"):
                issues.append("SQL file path is required")
            elif not self._sql_file_exists():
                issues.append("SQL file not found")
        if source == "SqlTemplate":
            date_error = sql.validate_date_range(
                self._input_value("start-date"), self._input_value("end-date")
            )
            if date_error:
                issues.append(date_error)
        existing_error: str | None = None
        existing = self._input_value("existing-table")
        if source == "ExistingTable":
            existing_error = sql.validate_full_table(existing, "Existing table")
            if existing_error:
                issues.append(existing_error)
        if destination in ("Csv", "Table+Csv"):
            csv_table = self._input_value("table-name")
            if source == "ExistingTable" and existing_error is None:
                _schema, csv_table = existing.split(".", 1)
            try:
                sql.safe_csv_path(self.launch_cwd, csv_table)
            except ValueError as exc:
                issues.append(str(exc))
        email = self._input_value("email")
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            issues.append("Invalid email format")
        if not jobs.can_launch():
            issues.append(f"At the {jobs.RUNNING_CAP}-Job concurrency cap")
        if self.kerberos_ttl is None:
            issues.append("Kerberos missing \u2014 press K to kinit")
        elif self.kerberos_ttl < 300:
            issues.append("Kerberos TTL under 5 min \u2014 press K to renew")
        return issues

    def _update_validation_summary(self) -> None:
        issues = self._validation_issues()
        summary = self.query_one("#validation-summary", Static)
        if issues:
            first = issues[0]
            extra = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
            summary.update(f"[red]\u2717 {len(issues)} issue(s): {first}{extra}[/]")
        else:
            summary.update("[green]\u2713 Ready to launch (checks passing)[/]")

    def _schedule_validation_summary(self) -> None:
        if self._validation_summary_timer is not None:
            self._validation_summary_timer.stop()
        self._validation_summary_timer = self.set_timer(0.2, self._run_scheduled_validation_summary)

    def _run_scheduled_validation_summary(self) -> None:
        self._validation_summary_timer = None
        self._update_validation_summary()

    def _update_field_visibility(self) -> None:
        source = self._selected_source()
        destination = self._selected_destination()

        dest_radio_set = self.query_one("#destination", RadioSet)
        for btn in dest_radio_set.query(RadioButton):
            dest_type = _DEST_IDS.get(btn.id or "", "")
            btn.disabled = (source, dest_type) not in manifest.LEGAL_CELLS
        self._ensure_legal_destination(source, dest_radio_set)

        is_sql = source in ("SqlFile", "SqlTemplate")
        is_existing = source == "ExistingTable"
        is_template = source == "SqlTemplate"
        needs_table = destination in ("Table", "Table+Csv") or is_template

        self.query_one("#row-sql-file").display = is_sql
        # A prefilled (re-run / test) form already has its SQL path chosen, so the
        # cwd file picker is redundant. Suppressing it also keeps the taller
        # table-producing forms within a single SSH-pane height so the Schema /
        # Table Name rows stay on screen without manual scrolling.
        show_picker = is_sql and bool(self._cwd_sql_files) and not self._prefilled
        self.query_one("#sql-file-picker").display = show_picker
        self.query_one("#picker-caption").display = show_picker
        self.query_one("#row-existing-table").display = is_existing
        self.query_one("#row-schema").display = needs_table
        self.query_one("#row-table-name").display = needs_table
        self.query_one("#row-start-date").display = is_template
        self.query_one("#row-end-date").display = is_template

        hint = self.query_one("#path-hint", Static)
        hint.display = is_sql
        if is_sql:
            self._refresh_path_hint()

        dest_hint = self.query_one("#dest-hint", Static)
        if source == "SqlTemplate":
            dest_hint.update("[dim]SqlTemplate supports Table only[/]")
            dest_hint.display = True
        elif source == "ExistingTable":
            dest_hint.update("[dim]ExistingTable supports Csv only[/]")
            dest_hint.display = True
        else:
            dest_hint.display = False

    def _ensure_legal_destination(self, source: str, dest_radio_set: RadioSet) -> None:
        """Auto-select the first legal destination when the current one is illegal."""
        if (source, self._selected_destination()) in manifest.LEGAL_CELLS:
            return
        for btn in dest_radio_set.query(RadioButton):
            dest_type = _DEST_IDS.get(btn.id or "", "")
            if (source, dest_type) in manifest.LEGAL_CELLS:
                # Deferred via timer: handlers of RadioSet.Changed run with
                # RadioButton.Changed prevented (Textual attaches the prevent
                # set to the message), which would silently skip the dest
                # set's exclusion logic. A timer callback runs prevention-free.
                self.set_timer(0.01, lambda b=btn: setattr(b, "value", True))
                return

    def _refresh_path_hint(self) -> None:
        """Update the path hint to show just the filename of the SQL file."""
        raw = self._input_value("sql-file")
        hint = self.query_one("#path-hint", Static)
        if raw:
            name = Path(raw).name
            exists = self._sql_file_exists()
            icon = "[green]\u2713[/]" if exists else "[red]\u2717[/]"
            hint.update(f"{icon} [dim]{name}[/]")
        else:
            hint.update("")

    def _refresh_kerberos(self) -> None:
        launch_btn = self.query_one("#launch", Button)
        launch_btn.disabled = self.kerberos_ttl is None or self.kerberos_ttl < 300
        self._update_validation_summary()

    def _on_kerberos_change(self, value: int | None) -> None:
        self.kerberos_ttl = value
        self._refresh_kerberos()
        self._inline_validate()
        self._update_validation_summary()

    def _read_sql(self) -> str | None:
        sql_path = Path(self._input_value("sql-file"))
        try:
            return sql_path.read_text(encoding="utf-8")
        except OSError as exc:
            self._show_message(f"Cannot read SQL file: {sql_path}\n{exc}", "error")
            return None

    def _show_message(self, text: str, severity: str = "info") -> None:
        widget = self.query_one("#warning-text", Static)
        markup_map = {
            "error": "red",
            "warning": "yellow",
            "success": "green",
            "info": "dim",
        }
        color = markup_map.get(severity, "dim")
        widget.update(f"[{color}]{text}[/]")

    def _detect_sql(self) -> None:
        content = self._read_sql()
        if content is None:
            return
        detected = sql.detect_source(content)
        info = self.query_one("#info-detected", Static)
        info.update(
            f"Detected source: [b]{detected}[/] \u00b7 illegal destinations are disabled automatically"
        )
        if detected == "SqlTemplate":
            self.query_one("#src-sqltemplate", RadioButton).value = True
        elif detected == "ExistingTable":
            self.query_one("#src-existingtable", RadioButton).value = True

    def _validate(self) -> str | None:
        source_type = self._selected_source()
        destination_type = self._selected_destination()
        if (source_type, destination_type) not in manifest.LEGAL_CELLS:
            return f"Illegal Source/Destination cell: {source_type}/{destination_type}"
        if destination_type in ("Table", "Table+Csv"):
            schema_error = sql.validate_identifier(self._input_value("schema"), "Schema")
            if schema_error:
                return schema_error
            table_error = sql.validate_identifier(self._input_value("table-name"), "Table name")
            if table_error:
                return table_error
        csv_table = self._input_value("table-name")
        if source_type == "ExistingTable":
            existing = self._input_value("existing-table")
            existing_error = sql.validate_full_table(existing, "Existing table")
            if existing_error:
                return existing_error
            _schema, csv_table = existing.split(".", 1)
        if destination_type in ("Csv", "Table+Csv"):
            try:
                sql.safe_csv_path(self.launch_cwd, csv_table)
            except ValueError as exc:
                return str(exc)
        email = self._input_value("email")
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            return "Invalid email format"
        if not jobs.can_launch():
            return f"Already at the {jobs.RUNNING_CAP}-Job concurrency cap; wait for one to finish"
        if self.kerberos_ttl is None:
            return "Kerberos ticket missing"
        if self.kerberos_ttl < 300:
            return "Kerberos ticket TTL is under 5 minutes"
        if source_type == "ExistingTable":
            return None
        sql_text = self._read_sql()
        if sql_text is None:
            return "SQL file is unreadable"
        if sql.is_malformed_template(sql_text):
            return "SQL contains only one of {date_inicio}/{date_fim} \u2014 likely a typo"
        if source_type == "SqlTemplate":
            if not sql.template_is_complete(sql_text):
                return "SqlTemplate requires both {date_inicio} and {date_fim}"
            date_error = sql.validate_date_range(
                self._input_value("start-date"), self._input_value("end-date")
            )
            if date_error:
                return date_error
        return None

    def _source_destination(self) -> tuple[manifest.Source, manifest.Destination]:
        source_type = self._selected_source()
        destination_type = self._selected_destination()
        schema = self._input_value("schema")
        table = self._input_value("table-name")
        if source_type == "ExistingTable":
            existing = self._input_value("existing-table") or f"{schema}.{table}"
            source: manifest.Source = {"type": "ExistingTable", "table_name": existing}
            if "." in existing:
                schema, table = existing.split(".", 1)
        else:
            source = {"type": source_type, "sql_path_at_launch": self._input_value("sql-file")}
        csv_path = str(sql.safe_csv_path(self.launch_cwd, table))
        destination: manifest.Destination = {
            "type": destination_type,
            "schema": schema,
            "table_name": table,
            "csv_path": csv_path,
        }
        return source, destination

    def _params(self) -> dict[str, str]:
        params = {"to_email": self._input_value("email"), "subject": self._input_value("subject")}
        if self._selected_source() == "SqlTemplate":
            params["start_date"] = sql.to_orchestrator_date(self._input_value("start-date"))
            params["end_date"] = sql.to_orchestrator_date(self._input_value("end-date"))
        return params

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "launch":
            self.action_launch()
        elif event.button.id == "preview":
            self.action_preview()

    def action_preview(self) -> None:
        source_type = self._selected_source()
        destination_type = self._selected_destination()
        schema = self._input_value("schema")
        table = self._input_value("table-name")
        if source_type == "ExistingTable":
            self._show_message("Preview is not available for ExistingTable source.", "warning")
            return
        sql_text = self._read_sql()
        if sql_text is None:
            return
        if source_type == "SqlTemplate":
            date_error = sql.validate_date_range(
                self._input_value("start-date"), self._input_value("end-date")
            )
            if date_error:
                self._show_message(date_error, "error")
                return
            preview = sql.monthly_preview(
                sql_text,
                schema,
                table,
                self._input_value("start-date"),
                self._input_value("end-date"),
            )
        elif destination_type in ("Table", "Table+Csv") and not sql.is_self_contained_ddl(sql_text):
            # Only table destinations are wrapped in DROP/CREATE ... AS, matching
            # manifest._effective_job_sql. A Csv-only job runs the raw SELECT, so
            # previewing the wrapper would misrepresent what executes.
            preview = sql.table_wrapper(sql_text, schema, table, config.current_user())
        else:
            # Csv destination, or a SqlFile that already carries its own DDL:
            # show the SQL verbatim (no double-wrapping).
            preview = sql_text
        self.app.push_screen(
            PreviewScreen(
                "SQL Preview",
                preview,
                schema=schema,
                table=table,
                source_type=source_type,
                dest_type=self._selected_destination(),
            )
        )

    def action_launch(self) -> Worker[None]:
        """Run the confirm-and-launch flow in a worker.

        Awaiting the confirmation future inline would block the message pump
        that dispatched the key binding and deadlock all input.
        """
        return self.run_worker(self._launch_flow(), name="launch-flow", exclusive=True)

    async def _launch_flow(self) -> None:
        error = self._validate()
        if error:
            telemetry.emit("launch_refused", {"reason": _refusal_reason(error)})
            self._show_message(error, "error")
            self.notify(error, severity="error")
            return
        source_type = self._selected_source()
        if source_type == "ExistingTable":
            sql_text = ""
        else:
            sql_text = self._read_sql()
            if sql_text is None:
                telemetry.emit("launch_refused", {"reason": "validation"})
                return
        source, destination = self._source_destination()
        confirmed = await self._confirm_launch(source, destination)
        if not confirmed:
            return
        if hasattr(self.app, "refresh_kerberos"):
            await self.app.refresh_kerberos()
        error = self._validate()
        if error:
            telemetry.emit("launch_refused", {"reason": _refusal_reason(error)})
            self._show_message(error, "error")
            self.notify(error, severity="error")
            return
        try:
            job_dir, _job_manifest = jobs.create_job_if_slot_available(
                source=source,
                destination=destination,
                params=self._params(),
                launch_cwd=self.launch_cwd,
                sql_text=sql_text,
            )
        except jobs.LaunchSlotUnavailable as exc:
            error = str(exc)
            telemetry.emit("launch_refused", {"reason": "slot_cap"})
            self._show_message(error, "error")
            self.notify(error, severity="error")
            return
        try:
            await process.launch_runner(job_dir)
        except OSError as exc:
            manifest.update(
                job_dir / "manifest.json",
                state="Failed",
                exit_code=-1,
                finished_at=manifest.now_utc(),
            )
            error = f"Could not launch detached runner: {exc}"
            logger.exception("Failed to launch runner for Job %s", job_dir.name)
            self._show_message(error, "error")
            self.notify(error, severity="error")
            return
        telemetry.emit(
            "job_launched",
            {
                "job_id": job_dir.name,
                "source": source["type"],
                "destination": destination["type"],
            },
        )
        logger.info(
            "Launched Job %s source=%s dest=%s", job_dir.name, source["type"], destination["type"]
        )
        self._save_form_defaults()
        self.notify(f"\u2713 Launched Job {job_dir.name}", severity="information")
        self._show_message(f"\u2713 Launched Job {job_dir.name}", "success")

    async def _confirm_launch(
        self,
        source: manifest.Source,
        destination: manifest.Destination,
    ) -> bool:
        loop_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def on_result(result: bool | None) -> None:
            if not loop_future.done():
                loop_future.set_result(bool(result))

        source_type = source["type"]
        source_detail = source.get("table_name") or source.get("sql_path_at_launch") or "--"
        dest_type = destination["type"]
        schema = destination.get("schema") or "--"
        table = destination.get("table_name") or "--"
        csv_path = destination.get("csv_path") or "--"
        body = (
            f"Source: [cyan]{source_type}[/]  {source_detail}\n"
            f"Destination: [cyan]{dest_type}[/]\n"
            f"Target table: [cyan]{schema}.{table}[/]\n"
            f"CSV path: {csv_path}\n"
            f"Email: {self._input_value('email') or '--'}"
        )
        self.app.push_screen(
            ConfirmScreen(
                "Launch Job",
                body,
                danger=True,
                confirm_label="Launch",
                cancel_label="Review",
            ),
            callback=on_result,
        )
        return await loop_future

    def action_edit_sql(self) -> None:
        editor = os.environ.get("EDITOR", "vi")
        with self.app.suspend():
            process.run_interactive(editor, self._input_value("sql-file"))
        self._sql_exists_cache = None  # the editor may have created the file
        self._detect_sql()

    async def action_kinit(self) -> None:
        with self.app.suspend():
            process.run_interactive("kinit")
        await self.app.refresh_kerberos()
        if self.kerberos_ttl is not None:
            self.notify(f"Kerberos refreshed: {self.kerberos_ttl // 60}m", severity="information")
        else:
            self.notify("Kerberos ticket still missing", severity="warning")

    def _apply_prefill(self) -> None:
        if not self.prefill:
            return
        # A prefilled (re-run / test) form already has its source and
        # destination chosen, so collapse the legal-cells reference matrix to
        # keep the whole form within a single screen height.
        try:
            self.query_one("#matrix-collapsible", Collapsible).collapsed = True
        except Exception:  # noqa: BLE001
            pass
        mapping = {
            "sql_file": "sql-file",
            "existing_table": "existing-table",
            "schema": "schema",
            "table_name": "table-name",
            "email": "email",
            "subject": "subject",
            "start_date": "start-date",
            "end_date": "end-date",
        }
        for key, widget_id in mapping.items():
            value = self.prefill.get(key, "")
            if value:
                self.query_one(f"#{widget_id}", Input).value = str(value)
        source_type = self.prefill.get("source_type")
        source_btn = {
            "SqlFile": "src-sqlfile",
            "SqlTemplate": "src-sqltemplate",
            "ExistingTable": "src-existingtable",
        }.get(source_type or "")
        dest_type = self.prefill.get("dest_type")
        dest_btn = {
            "Table": "dst-table",
            "Csv": "dst-csv",
            "Table+Csv": "dst-table-csv",
        }.get(dest_type or "")
        # Select the radios after mount settles. Setting ``button.value = True``
        # relies on an async RadioButton.Changed message that races with
        # RadioSet._on_mount pinning the default-pressed button; on Textual
        # 8.2.5 this intermittently leaves the default selected. We instead
        # force the set's selection state deterministically, after refresh so
        # the set has finished its own mount.
        if source_btn:
            self._schedule_force_radio("#source", source_btn)
        if dest_btn:
            self._schedule_force_radio("#destination", dest_btn)
        # After the radios settle, scroll the destination-specific fields into
        # view so a prefilled (re-run) form lands on its key inputs.
        self.call_after_refresh(self._scroll_prefill_fields)

    def _schedule_force_radio(self, radio_set_id: str, button_id: str) -> None:
        self.call_after_refresh(self._force_radio, radio_set_id, button_id)
        self.set_timer(0.05, lambda: self._force_radio(radio_set_id, button_id))
        self.set_timer(0.25, lambda: self._force_radio(radio_set_id, button_id))

    def _force_radio(self, radio_set_id: str, button_id: str) -> None:
        radio_set = self.query_one(radio_set_id, RadioSet)
        target = self.query_one(f"#{button_id}", RadioButton)
        with self.prevent(RadioButton.Changed):
            for btn in radio_set.query(RadioButton):
                btn.value = btn is target
        radio_set._pressed_button = target
        nodes = list(radio_set._nodes)
        if target in nodes:
            radio_set._selected = nodes.index(target)
        logger.info(
            "prefill applied %s -> %s (pressed=%s)",
            radio_set_id,
            button_id,
            radio_set.pressed_button.id if radio_set.pressed_button else None,
        )
        self._update_field_visibility()
        self._inline_validate()
        self._update_validation_summary()

    def _scroll_prefill_fields(self) -> None:
        """Bring the destination-dependent rows into the viewport for a
        prefilled form so they are visible without manual scrolling."""
        for wid in ("#row-table-name", "#row-schema", "#row-sql-file"):
            try:
                row = self.query_one(wid)
            except Exception:  # noqa: BLE001
                continue
            if row.display:
                row.scroll_visible(animate=False)
                return

    def _apply_saved_defaults(self) -> None:
        defaults = config.read_form_defaults()
        if not defaults:
            return
        field_map = {
            "schema": "schema",
            "email": "email",
            "subject": "subject",
        }
        for key, widget_id in field_map.items():
            if key in defaults and defaults[key]:
                try:
                    self.query_one(f"#{widget_id}", Input).value = defaults[key]
                except Exception:
                    pass

    def _save_form_defaults(self) -> None:
        values = {
            "schema": self._input_value("schema"),
            "email": self._input_value("email"),
            "subject": self._input_value("subject"),
            "destination_type": self._selected_destination(),
        }
        try:
            config.save_form_defaults(values)
        except OSError:
            pass

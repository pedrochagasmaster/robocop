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
    SelectionList,
    Static,
)
from textual.worker import Worker

from .. import config, job_ops, kerberos, manifest, process, sql, telemetry
from ..advisor import analyze, analyze_form, analyze_sql, combine_analysis
from ..advisor.models import AnalysisResult, badge_markup
from .advisor_gate import AdvisorLaunchGate
from .confirm import ConfirmScreen
from .preview import PreviewScreen
from .sidebar import Sidebar

logger = logging.getLogger("dispatch.new_job")


_SOURCE_IDS = {
    "src-sqlfile": "SqlFile",
    "src-sqltemplate": "SqlTemplate",
    "src-existingtable": "ExistingTable",
}
_DEST_IDS = {"dst-table": "Table", "dst-csv": "Csv", "dst-table-csv": "Table+Csv"}
_EXISTING_SCHEMA_IDS = {
    "esc-coe-enc": "coe_enc",
    "esc-aa-enc": "aa_enc",
    "esc-other": "other",
}
_KNOWN_EXISTING_SCHEMAS = frozenset({"coe_enc", "aa_enc"})

# Execution-queue (Impala request pool) selection. The user may pick one or
# more queues; the job then cycles through the chosen pools (in the order shown
# below) via DISPATCH_REQUEST_POOL. Selecting nothing keeps the original
# behaviour: the orchestrators cycle their own hardcoded queue list.
# See scr/_common.resolve_pools and dispatch/runner._orchestrator_env.
_QUEUE_AUTO = job_ops.QUEUE_AUTO
_QUEUE_CHOICES: list[tuple[str, str]] = [
    ("adhoc_fast \u00b7 fastest, short / simple queries", "adhoc_fast"),
    ("adhoc_small \u00b7 small queries", "adhoc_small"),
    ("acs_small \u00b7 ACS small pool", "acs_small"),
    ("acs_large \u00b7 large / heavy queries", "acs_large"),
    ("adhoc \u00b7 general, long-running queries", "adhoc"),
]
_QUEUE_ORDER = list(job_ops.QUEUE_ORDER)
_QUEUE_VALUES = set(job_ops.QUEUE_VALUES)
_QUEUE_HINTS = {
    "adhoc_fast": "Fast pool \u2014 best for short or simple queries.",
    "adhoc_small": "Small pool \u2014 light queries with a modest footprint.",
    "acs_small": "ACS small pool \u2014 light ACS workloads.",
    "acs_large": "Large pool \u2014 heavy or long-running queries.",
    "adhoc": "General pool \u2014 long-running or resource-heavy queries.",
}
_QUEUE_AUTO_HINT = (
    "None selected \u2192 Auto: cycle every queue until one accepts (default). "
    "Pick one or more to restrict; multiple are tried in order."
)


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
        self._eid = config.current_user()
        self.kerberos_ttl: int | None = None
        self._matrix_collapsed = bool(config.read_form_defaults())
        self._prefilled = bool(self.prefill)
        prefill_source = str(self.prefill.get("source_type") or "SqlFile")
        self._prefill_source = (
            prefill_source
            if prefill_source in {"SqlFile", "SqlTemplate", "ExistingTable"}
            else "SqlFile"
        )
        prefill_destination = str(self.prefill.get("dest_type") or "Csv")
        self._prefill_destination = (
            prefill_destination if prefill_destination in {"Table", "Csv", "Table+Csv"} else "Csv"
        )
        existing_table = str(self.prefill.get("existing_table") or "")
        self._prefill_existing_schema = (
            existing_table.split(".", 1)[0] if "." in existing_table else "aa_enc"
        )
        self._cwd_sql_files: list[dict] = []
        self._picker_ready = False
        self._sql_exists_cache: tuple[str, bool] | None = None
        self._sql_analysis_cache: tuple[tuple[str, str, str], AnalysisResult] | None = None
        self._validation_summary_timer: Timer | None = None
        self._live_validation_generation = 0

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
                                yield RadioButton(
                                    "SqlFile",
                                    value=self._prefill_source == "SqlFile",
                                    id="src-sqlfile",
                                )
                                yield RadioButton(
                                    manifest.source_display_label("SqlTemplate"),
                                    value=self._prefill_source == "SqlTemplate",
                                    id="src-sqltemplate",
                                )
                                yield RadioButton(
                                    "ExistingTable",
                                    value=self._prefill_source == "ExistingTable",
                                    id="src-existingtable",
                                )
                        with Vertical(classes="radio-group"):
                            yield Static("Destination", classes="field-label")
                            with RadioSet(id="destination"):
                                yield RadioButton(
                                    "Table",
                                    value=self._prefill_destination == "Table",
                                    id="dst-table",
                                )
                                yield RadioButton(
                                    "Csv", value=self._prefill_destination == "Csv", id="dst-csv"
                                )
                                yield RadioButton(
                                    "Table+Csv",
                                    value=self._prefill_destination == "Table+Csv",
                                    id="dst-table-csv",
                                )
                    yield Static("", id="dest-hint")
                with Vertical(id="queue-panel"):
                    yield Static(
                        "Execution Queue (select one or more)",
                        classes="field-label",
                        id="lbl-queue",
                    )
                    yield SelectionList[str](*_QUEUE_CHOICES, id="queue")
                    yield Static(_QUEUE_AUTO_HINT, id="queue-hint", classes="input-caption")
                yield Static("", id="picker-caption", classes="input-caption")
                yield DataTable(id="sql-file-picker")
                with Vertical(id="form-grid"):
                    with Horizontal(classes="form-row", id="row-sql-file"):
                        yield Static("SQL File", classes="field-label", id="lbl-sql-file")
                        yield Input(
                            value=self._default_sql_file(), placeholder="SQL File", id="sql-file"
                        )
                    yield Static("", classes="path-hint", id="path-hint")
                    with Horizontal(classes="form-row", id="row-existing-schema"):
                        yield Static("Schema", classes="field-label", id="lbl-existing-schema")
                        with RadioSet(id="existing-schema"):
                            yield RadioButton(
                                "coe_enc",
                                value=self._prefill_existing_schema == "coe_enc",
                                id="esc-coe-enc",
                            )
                            yield RadioButton(
                                "aa_enc",
                                value=self._prefill_existing_schema == "aa_enc",
                                id="esc-aa-enc",
                            )
                            yield RadioButton(
                                "other",
                                value=self._prefill_existing_schema not in _KNOWN_EXISTING_SCHEMAS,
                                id="esc-other",
                            )
                    with Horizontal(classes="form-row", id="row-existing-schema-custom"):
                        yield Static(
                            "Custom Schema", classes="field-label", id="lbl-existing-schema-custom"
                        )
                        yield Input(value="", placeholder="Schema", id="existing-schema-custom")
                    with Horizontal(classes="form-row", id="row-existing-table"):
                        yield Static(
                            "Existing Table", classes="field-label", id="lbl-existing-table"
                        )
                        yield Input(
                            value="", placeholder="e.g. events_existing", id="existing-table"
                        )
                    with Horizontal(classes="form-row", id="row-schema"):
                        yield Static("Schema", classes="field-label", id="lbl-schema")
                        yield Input(value="aa_enc", placeholder="Schema", id="schema")
                    with Horizontal(classes="form-row", id="row-table-name"):
                        yield Static("Table Name", classes="field-label", id="lbl-table-name")
                        with Horizontal(classes="eid-table-name-field"):
                            yield Static(
                                sql.eid_table_prefix(self._eid),
                                id="table-name-prefix",
                                classes="input-prefix",
                            )
                            yield Input(
                                value="dispatch_result",
                                placeholder="suffix",
                                id="table-name-suffix",
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
                            placeholder=os.environ.get(
                                "DISPATCH_EMAIL",
                                "name.surname@mastercard.com,name2.surname2@mastercard.com",
                            ),
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
        matrix.add_row(
            manifest.source_display_label("SqlTemplate"),
            "[green]\u2713[/]",
            "[dim]\u2014[/]",
            "[dim]\u2014[/]",
        )
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
        self._update_field_visibility()
        self._inline_validate()
        self.query_one("#validation-summary", Static).update("[dim]Checking\u2026[/]")
        self._schedule_validation_summary(delay=0.0)
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
        self._cwd_sql_files = await asyncio.to_thread(self._scan_cwd_sql_files)
        picker = self.query_one("#sql-file-picker", DataTable)
        picker.clear()
        for entry in self._cwd_sql_files:
            detected = entry["detected"]
            detected_markup = (
                f"[cyan]{manifest.source_display_label(detected)}[/]"
                if detected == "SqlTemplate"
                else detected
            )
            picker.add_row(
                entry["name"], detected_markup, f"[dim]{entry['mtime']}[/]", key=entry["path"]
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

    def _table_name_suffix_input(self) -> Input:
        return self.query_one("#table-name-suffix", Input)

    def _table_name_field_active(self) -> bool:
        source = self._selected_source()
        destination = self._selected_destination()
        return destination in ("Table", "Table+Csv") or source == "SqlTemplate"

    def _table_name_value(self) -> str:
        suffix = self._normalize_table_name_suffix(self._table_name_suffix_input().value)
        if self._table_name_field_active():
            return sql.join_eid_table_name(self._eid, suffix)
        return suffix

    def _set_table_name_suffix(self, value: str) -> None:
        self._table_name_suffix_input().value = sql.split_eid_table_suffix(value, self._eid)

    def _normalize_table_name_suffix(self, raw: str) -> str:
        return sql.split_eid_table_suffix(raw, self._eid).strip()

    @staticmethod
    def _pressed_radio_id(radio_set: RadioSet) -> str | None:
        for btn in radio_set.query(RadioButton):
            if btn.value and btn.id:
                return btn.id
        if radio_set.pressed_button is not None and radio_set.pressed_button.id:
            return radio_set.pressed_button.id
        return None

    def _selected_source(self) -> str:
        radio_set = self.query_one("#source", RadioSet)
        button_id = self._pressed_radio_id(radio_set)
        return _SOURCE_IDS.get(button_id, "SqlFile") if button_id is not None else "SqlFile"

    def _selected_destination(self) -> str:
        radio_set = self.query_one("#destination", RadioSet)
        button_id = self._pressed_radio_id(radio_set)
        return _DEST_IDS.get(button_id, "Csv") if button_id is not None else "Csv"

    def _selected_existing_schema_choice(self) -> str:
        radio_set = self.query_one("#existing-schema", RadioSet)
        button_id = self._pressed_radio_id(radio_set)
        return _EXISTING_SCHEMA_IDS.get(button_id, "aa_enc") if button_id is not None else "aa_enc"

    def _existing_table_schema(self) -> str:
        choice = self._selected_existing_schema_choice()
        return (
            choice
            if choice in _KNOWN_EXISTING_SCHEMAS
            else self._input_value("existing-schema-custom")
        )

    def _existing_full_table(self) -> str:
        schema = self._existing_table_schema()
        table = self._input_value("existing-table")
        return f"{schema}.{table}" if schema and table else table

    def _selected_queues(self) -> list[str]:
        try:
            selection = self.query_one("#queue", SelectionList)
        except Exception:
            return []
        chosen = {str(value) for value in selection.selected}
        return [queue for queue in _QUEUE_ORDER if queue in chosen]

    def _queue_param(self) -> str:
        queues = self._selected_queues()
        return ",".join(queues) if queues else _QUEUE_AUTO

    def _update_queue_hint(self) -> None:
        queues = self._selected_queues()
        hint = self.query_one("#queue-hint", Static)
        if not queues:
            hint.update(_QUEUE_AUTO_HINT)
        elif len(queues) == 1:
            hint.update(_QUEUE_HINTS.get(queues[0], ""))
        else:
            hint.update("Tried in order: " + " \u2192 ".join(queues))

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._update_field_visibility()
        self._inline_validate()
        self._schedule_validation_summary()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        if event.control.id == "queue":
            self._update_queue_hint()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "table-name-suffix":
            normalized = self._normalize_table_name_suffix(event.value)
            if normalized != event.value:
                event.input.value = normalized
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
        raw = self._input_value("sql-file")
        cached = self._sql_exists_cache
        if cached is not None and cached[0] == raw:
            return cached[1]
        exists = Path(raw).exists() if raw else False
        self._sql_exists_cache = (raw, exists)
        return exists

    def _inline_validate(self) -> None:
        """Paint cheap validation only; filesystem checks arrive asynchronously."""
        source = self._selected_source()
        msgs = []
        if source in ("SqlFile", "SqlTemplate"):
            raw = self._input_value("sql-file")
            cached = self._sql_exists_cache
            if cached is not None and cached[0] == raw:
                if cached[1]:
                    msgs.append("[green]\u2713[/] SQL file found")
                elif raw:
                    msgs.append("[red]\u2717[/] SQL file not found")
            elif raw:
                msgs.append("[dim]\u2026 SQL file checking[/]")
        email = self._input_value("email")
        if email:
            msgs.append(
                "[green]\u2713[/] Email"
                if "@" in email and "." in email.split("@")[-1]
                else "[red]\u2717[/] Invalid email format"
            )
        if self.kerberos_ttl is None:
            msgs.append("[red]\u2717[/] Kerberos missing")
        elif self.kerberos_ttl < kerberos.MIN_LAUNCH_TTL_SECONDS:
            msgs.append("[yellow]\u26a0[/] Kerberos TTL low")
        else:
            msgs.append("[green]\u2713[/] Kerberos")
        self.query_one("#warning-text", Static).update("  ".join(msgs))

    def _launch_inputs(self) -> job_ops.LaunchInputs:
        return job_ops.LaunchInputs(
            source_type=self._selected_source(),
            destination_type=self._selected_destination(),
            launch_cwd=self.launch_cwd,
            sql_path=self._input_value("sql-file"),
            existing_table=self._existing_full_table(),
            schema=self._input_value("schema"),
            table_name=self._table_name_value(),
            start_date=self._input_value("start-date"),
            end_date=self._input_value("end-date"),
            email=self._input_value("email"),
            subject=self._input_value("subject"),
            queue=self._queue_param(),
            user=self._eid,
        )

    def _validation_issues(self, *, deep: bool = False) -> list[str]:
        issues: list[str] = []
        if (
            self._selected_source() == "ExistingTable"
            and self._selected_existing_schema_choice() == "other"
        ):
            schema_error = sql.validate_identifier(
                self._input_value("existing-schema-custom"), "Schema"
            )
            if schema_error:
                issues.append(schema_error)
        shared = job_ops.validation_issues(
            self._launch_inputs(), kerberos_ttl=self.kerberos_ttl, deep=deep
        )
        for issue in shared:
            if issue == job_ops.MSG_KERBEROS_MISSING:
                issues.append("Kerberos ticket missing \u2014 press K to kinit")
            elif issue == job_ops.MSG_KERBEROS_TTL_SHORT:
                issues.append("Kerberos ticket TTL is under 5 minutes \u2014 press K to renew")
            else:
                issues.append(issue)
        return issues

    def _update_validation_summary(self) -> None:
        self._apply_validation_summary(self._validation_issues(), self._current_analysis())

    def _current_analysis(self) -> AnalysisResult:
        source_type = self._selected_source()
        destination_type = self._selected_destination()
        table = self._table_name_value()
        user_id = config.current_user()
        sql_path = "" if source_type == "ExistingTable" else self._input_value("sql-file")
        form_result = analyze_form(
            source_type=source_type,
            destination_type=destination_type,
            destination_table=table,
            user_id=user_id,
        )
        sql_result = self._current_sql_analysis(source_type, user_id, sql_path)
        return combine_analysis(sql_result, form_result)

    def _current_sql_analysis(
        self, source_type: str, user_id: str, sql_path: str
    ) -> AnalysisResult:
        cache_key = (source_type, user_id, sql_path)
        if self._sql_analysis_cache is not None and self._sql_analysis_cache[0] == cache_key:
            return self._sql_analysis_cache[1]
        result = self._compute_sql_analysis(source_type, user_id, sql_path)
        self._sql_analysis_cache = (cache_key, result)
        return result

    def _compute_sql_analysis(
        self, source_type: str, user_id: str, sql_path: str
    ) -> AnalysisResult:
        if source_type == "ExistingTable":
            return analyze_sql("", source_type=source_type, user_id=user_id)
        if not sql_path or not self._sql_file_exists():
            return AnalysisResult(available=True, findings=())
        try:
            sql_text = Path(sql_path).read_text(encoding="utf-8")
        except OSError:
            return AnalysisResult(available=True, findings=())
        return analyze_sql(sql_text, source_type=source_type, user_id=user_id)

    def _schedule_validation_summary(self, *, delay: float = 0.15) -> None:
        self._live_validation_generation += 1
        if self._validation_summary_timer is not None:
            self._validation_summary_timer.stop()
        self._validation_summary_timer = self.set_timer(delay, self._start_validation_worker)

    def _start_validation_worker(self) -> None:
        self._validation_summary_timer = None
        generation = self._live_validation_generation
        inputs = self._launch_inputs()
        custom_schema_choice = self._selected_existing_schema_choice()
        custom_schema = self._input_value("existing-schema-custom")
        ttl = self.kerberos_ttl
        sql_cache_key = (inputs.source_type, inputs.user or self._eid, inputs.sql_path)
        cached_sql = None
        if self._sql_analysis_cache is not None and self._sql_analysis_cache[0] == sql_cache_key:
            cached_sql = self._sql_analysis_cache[1]
        self.run_worker(
            self._refresh_validation_async(
                generation,
                inputs,
                custom_schema_choice,
                custom_schema,
                ttl,
                sql_cache_key,
                cached_sql,
            ),
            name="new-job-live-validation",
            group="new-job-validation",
            exclusive=True,
        )

    async def _refresh_validation_async(
        self,
        generation: int,
        inputs: job_ops.LaunchInputs,
        custom_schema_choice: str,
        custom_schema: str,
        ttl: int | None,
        sql_cache_key: tuple[str, str, str],
        cached_sql: AnalysisResult | None,
    ) -> None:
        def compute() -> tuple[list[str], AnalysisResult, bool | None, AnalysisResult, str | None]:
            issues: list[str] = []
            if inputs.source_type == "ExistingTable" and custom_schema_choice == "other":
                schema_error = sql.validate_identifier(custom_schema, "Schema")
                if schema_error:
                    issues.append(schema_error)
            shared = job_ops.validation_issues(inputs, kerberos_ttl=ttl, deep=False)
            for issue in shared:
                if issue == job_ops.MSG_KERBEROS_MISSING:
                    issues.append("Kerberos ticket missing \u2014 press K to kinit")
                elif issue == job_ops.MSG_KERBEROS_TTL_SHORT:
                    issues.append("Kerberos ticket TTL is under 5 minutes \u2014 press K to renew")
                else:
                    issues.append(issue)
            file_exists: bool | None = None
            if inputs.source_type in ("SqlFile", "SqlTemplate") and inputs.sql_path.strip():
                file_exists = "SQL file not found" not in shared
            detected_source: str | None = None
            if cached_sql is not None:
                sql_result = cached_sql
            elif inputs.source_type == "ExistingTable":
                sql_result = analyze_sql("", source_type=inputs.source_type, user_id=inputs.user)
            elif file_exists:
                path = job_ops.resolve_sql_path(inputs.launch_cwd, inputs.sql_path)
                try:
                    sql_text = path.read_text(encoding="utf-8")
                except OSError:
                    sql_result = AnalysisResult(available=True, findings=())
                else:
                    detected_source = sql.detect_source(sql_text)
                    sql_result = analyze_sql(
                        sql_text, source_type=inputs.source_type, user_id=inputs.user
                    )
            else:
                sql_result = AnalysisResult(available=True, findings=())
            form_result = analyze_form(
                source_type=inputs.source_type,
                destination_type=inputs.destination_type,
                destination_table=inputs.table_name,
                user_id=inputs.user,
            )
            return (
                issues,
                combine_analysis(sql_result, form_result),
                file_exists,
                sql_result,
                detected_source,
            )

        issues, analysis, file_exists, sql_result, detected_source = await asyncio.to_thread(
            compute
        )
        if generation != self._live_validation_generation or not self.is_mounted:
            return
        if self._launch_inputs() != inputs or self.kerberos_ttl != ttl:
            return
        if file_exists is not None:
            self._sql_exists_cache = (inputs.sql_path, file_exists)
        if (
            detected_source in {"SqlTemplate", "ExistingTable"}
            and detected_source != inputs.source_type
        ):
            self._apply_detected_source(detected_source)
            return
        self._sql_analysis_cache = (sql_cache_key, sql_result)
        if detected_source is not None:
            self._apply_detected_source(detected_source)
        self._inline_validate()
        self._refresh_path_hint()
        self._apply_validation_summary(issues, analysis)

    def _apply_validation_summary(self, issues: list[str], analysis: AnalysisResult) -> None:
        summary = self.query_one("#validation-summary", Static)
        badge = badge_markup(analysis)
        if issues:
            first = issues[0]
            extra = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
            summary.update(
                f"[red]\u2717 {len(issues)} issue(s): {first}{extra}[/]  \u00b7  {badge}"
            )
        else:
            summary.update(f"[green]\u2713 Ready to launch[/]  \u00b7  {badge}")

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
        show_picker = is_sql and bool(self._cwd_sql_files) and not self._prefilled
        self.query_one("#sql-file-picker").display = show_picker
        self.query_one("#picker-caption").display = show_picker
        existing_schema_choice = self._selected_existing_schema_choice()
        show_existing_custom_schema = is_existing and existing_schema_choice == "other"
        self.query_one("#row-existing-schema").display = is_existing
        self.query_one("#row-existing-schema-custom").display = show_existing_custom_schema
        self.query_one("#row-existing-table").display = is_existing
        self.query_one("#existing-schema-custom", Input).disabled = not show_existing_custom_schema
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
            dest_hint.update(
                f"[dim]{manifest.source_display_label('SqlTemplate')} supports Table only[/]"
            )
            dest_hint.display = True
        elif source == "ExistingTable":
            dest_hint.update("[dim]ExistingTable supports Csv only[/]")
            dest_hint.display = True
        else:
            dest_hint.display = False

    def _ensure_legal_destination(self, source: str, dest_radio_set: RadioSet) -> None:
        if (source, self._selected_destination()) in manifest.LEGAL_CELLS:
            return
        for btn in dest_radio_set.query(RadioButton):
            dest_type = _DEST_IDS.get(btn.id or "", "")
            if (source, dest_type) in manifest.LEGAL_CELLS:
                self.set_timer(0.01, lambda b=btn: setattr(b, "value", True))
                return

    def _refresh_path_hint(self) -> None:
        raw = self._input_value("sql-file")
        hint = self.query_one("#path-hint", Static)
        if raw:
            name = Path(raw).name
            cached = self._sql_exists_cache
            if cached is not None and cached[0] == raw:
                icon = "[green]\u2713[/]" if cached[1] else "[red]\u2717[/]"
            else:
                icon = "[dim]\u2026[/]"
            hint.update(f"{icon} [dim]{name}[/]")
        else:
            hint.update("")

    def _refresh_kerberos(self) -> None:
        self.query_one("#launch", Button).disabled = (
            self.kerberos_ttl is None or self.kerberos_ttl < kerberos.MIN_LAUNCH_TTL_SECONDS
        )
        self._schedule_validation_summary()

    def _on_kerberos_change(self, value: int | None) -> None:
        self.kerberos_ttl = value
        self._refresh_kerberos()
        self._inline_validate()

    def _read_sql(self) -> str | None:
        sql_path = Path(self._input_value("sql-file"))
        try:
            return sql_path.read_text(encoding="utf-8")
        except OSError as exc:
            self._show_message(f"Cannot read SQL file: {sql_path}\n{exc}", "error")
            return None

    def _show_message(self, text: str, severity: str = "info") -> None:
        widget = self.query_one("#warning-text", Static)
        color = {"error": "red", "warning": "yellow", "success": "green", "info": "dim"}.get(
            severity, "dim"
        )
        widget.update(f"[{color}]{text}[/]")

    def _apply_detected_source(self, detected: str) -> None:
        info = self.query_one("#info-detected", Static)
        info.update(
            f"Detected source: [b]{manifest.source_display_label(detected)}[/] \u00b7 illegal destinations are disabled automatically"
        )
        if detected == "SqlTemplate":
            self.query_one("#src-sqltemplate", RadioButton).value = True
        elif detected == "ExistingTable":
            self.query_one("#src-existingtable", RadioButton).value = True

    def _validate(self) -> str | None:
        issues = self._validation_issues(deep=True)
        return issues[0] if issues else None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "launch":
            self.action_launch()
        elif event.button.id == "preview":
            self.action_preview()

    def action_preview(self) -> None:
        source_type = self._selected_source()
        destination_type = self._selected_destination()
        schema = self._input_value("schema")
        table = self._table_name_value()
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
            preview = sql.table_wrapper(sql_text, schema, table, config.current_user())
        else:
            preview = sql_text
        analysis = analyze(
            sql_text,
            source_type=source_type,
            destination_type=destination_type,
            destination_table=table,
            user_id=config.current_user(),
        )
        self.app.push_screen(
            PreviewScreen(
                "SQL Preview",
                preview,
                schema=schema,
                table=table,
                source_type=source_type,
                dest_type=self._selected_destination(),
                analysis=analysis,
            )
        )

    def action_launch(self) -> Worker[None]:
        return self.run_worker(self._launch_flow(), name="launch-flow", exclusive=True)

    async def _launch_flow(self) -> None:
        error = self._validate()
        if error:
            telemetry.note_launch_refused(job_ops.refusal_reason(error))
            self._show_message(error, "error")
            self.notify(error, severity="error")
            return
        try:
            plan = job_ops.prepare_launch(self._launch_inputs(), kerberos_ttl=self.kerberos_ttl)
        except (job_ops.ValidationError, job_ops.OperationalError) as exc:
            error = str(exc)
            self._show_message(error, "error")
            self.notify(error, severity="error")
            return
        errors = plan.analysis.errors()
        if errors:
            if not await self._confirm_advisor_gate(errors, plan.source, plan.destination):
                return
        elif not await self._confirm_launch(plan.source, plan.destination):
            return
        if hasattr(self.app, "refresh_kerberos"):
            await self.app.refresh_kerberos()
        error = None
        issues = job_ops.validation_issues(
            self._launch_inputs(), kerberos_ttl=self.kerberos_ttl, deep=False
        )
        if issues:
            issue = issues[0]
            if issue == job_ops.MSG_KERBEROS_MISSING:
                error = "Kerberos ticket missing \u2014 press K to kinit"
            elif issue == job_ops.MSG_KERBEROS_TTL_SHORT:
                error = "Kerberos ticket TTL is under 5 minutes \u2014 press K to renew"
            else:
                error = issue
        if error:
            telemetry.note_launch_refused(job_ops.refusal_reason(error))
            self._show_message(error, "error")
            self.notify(error, severity="error")
            return
        try:
            result = await job_ops.execute_launch_async(plan)
        except job_ops.OperationalError as exc:
            error = str(exc)
            self._show_message(error, "error")
            self.notify(error, severity="error")
            return
        if result.handoff_failed:
            error = result.handoff_error or "Could not launch detached runner"
            self._show_message(error, "error")
            self.notify(error, severity="error")
            return
        self._save_form_defaults()
        self.notify(f"\u2713 Launched Job {result.job_id}", severity="information")
        self._show_message(f"\u2713 Launched Job {result.job_id}", "success")

    def _launch_summary(self, source: manifest.Source, destination: manifest.Destination) -> str:
        source_type = source["type"]
        source_detail = source.get("table_name") or source.get("sql_path_at_launch") or "--"
        dest_type = destination["type"]
        schema = destination.get("schema") or "--"
        table = destination.get("table_name") or "--"
        csv_path = destination.get("csv_path") or "--"
        queues = self._selected_queues()
        pool_label = ", ".join(queues) if queues else "Auto (cycle all Resource Pools)"
        return (
            f"Source: [cyan]{manifest.source_display_label(source_type)}[/]  {source_detail}\n"
            f"Destination: [cyan]{dest_type}[/]\n"
            f"Target table: [cyan]{schema}.{table}[/]\n"
            f"Resource Pool: [cyan]{pool_label}[/]\n"
            f"CSV path: {csv_path}\n"
            f"Email: {self._input_value('email') or '--'}"
        )

    async def _confirm_advisor_gate(
        self, errors: tuple, source: manifest.Source, destination: manifest.Destination
    ) -> bool:
        return bool(
            await self.app.push_screen_wait(
                AdvisorLaunchGate(errors, job_summary=self._launch_summary(source, destination))
            )
        )

    async def _confirm_launch(
        self, source: manifest.Source, destination: manifest.Destination
    ) -> bool:
        result = await self.app.push_screen_wait(
            ConfirmScreen(
                "Launch Job",
                self._launch_summary(source, destination),
                danger=True,
                confirm_label="Launch",
                cancel_label="Review",
            )
        )
        return bool(result)

    def action_edit_sql(self) -> None:
        editor = os.environ.get("EDITOR", "vi")
        with self.app.suspend():
            process.run_interactive(editor, self._input_value("sql-file"))
        self._sql_exists_cache = None
        self._sql_analysis_cache = None
        self._inline_validate()
        self._schedule_validation_summary(delay=0.0)

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
        try:
            self.query_one("#matrix-collapsible", Collapsible).collapsed = True
        except Exception:
            pass
        mapping = {
            "sql_file": "sql-file",
            "schema": "schema",
            "email": "email",
            "subject": "subject",
            "start_date": "start-date",
            "end_date": "end-date",
        }
        for key, widget_id in mapping.items():
            value = self.prefill.get(key, "")
            if value:
                self.query_one(f"#{widget_id}", Input).value = str(value)
        table_name = self.prefill.get("table_name", "")
        if table_name:
            self._set_table_name_suffix(str(table_name))
        existing_table = self.prefill.get("existing_table", "")
        if existing_table:
            self._apply_existing_table_prefill(str(existing_table))
        self._apply_queue_value(self.prefill.get("queue", ""))
        source_btn = {
            "SqlFile": "src-sqlfile",
            "SqlTemplate": "src-sqltemplate",
            "ExistingTable": "src-existingtable",
        }.get(self._prefill_source)
        dest_btn = {"Table": "dst-table", "Csv": "dst-csv", "Table+Csv": "dst-table-csv"}.get(
            self._prefill_destination
        )
        if source_btn:
            self._schedule_force_radio("#source", source_btn)
        if dest_btn:
            self._schedule_force_radio("#destination", dest_btn)
        self.call_after_refresh(self._scroll_prefill_fields)

    def _apply_existing_table_prefill(self, existing_table: str) -> None:
        if "." not in existing_table:
            self.query_one("#existing-table", Input).value = existing_table
            return
        schema_part, table_part = existing_table.split(".", 1)
        self.query_one("#existing-table", Input).value = table_part
        if schema_part in _KNOWN_EXISTING_SCHEMAS:
            self._schedule_force_radio(
                "#existing-schema", {"coe_enc": "esc-coe-enc", "aa_enc": "esc-aa-enc"}[schema_part]
            )
            return
        self._schedule_force_radio("#existing-schema", "esc-other")
        self.query_one("#existing-schema-custom", Input).value = schema_part

    def _schedule_force_radio(self, radio_set_id: str, button_id: str) -> None:
        self.call_after_refresh(self._force_radio, radio_set_id, button_id)
        self.set_timer(0.05, lambda: self._force_radio(radio_set_id, button_id))
        self.set_timer(0.25, lambda: self._force_radio(radio_set_id, button_id))

    def _force_radio(self, radio_set_id: str, button_id: str) -> None:
        try:
            radio_set = self.query_one(radio_set_id, RadioSet)
            target = self.query_one(f"#{button_id}", RadioButton)
        except Exception:
            return
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
        self._schedule_validation_summary()

    def _scroll_prefill_fields(self) -> None:
        for wid in ("#row-table-name", "#row-schema", "#row-sql-file"):
            try:
                row = self.query_one(wid)
            except Exception:
                continue
            if row.display:
                row.scroll_visible(animate=False)
                return

    def _apply_queue_value(self, value: str) -> None:
        try:
            selection = self.query_one("#queue", SelectionList)
        except Exception:
            return
        queues = [
            token.strip() for token in str(value).split(",") if token.strip() in _QUEUE_VALUES
        ]
        with selection.prevent(SelectionList.SelectedChanged):
            selection.deselect_all()
            for queue in queues:
                selection.select(queue)
        self._update_queue_hint()

    def _apply_saved_defaults(self) -> None:
        defaults = config.read_form_defaults()
        if not defaults:
            return
        for key, widget_id in {"schema": "schema", "email": "email", "subject": "subject"}.items():
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

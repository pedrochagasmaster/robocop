from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"pattern not found in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Browser: paint the shell immediately and patch selection cells instead of
# rebuilding the complete DataTable for a one-row checkbox change.
replace(
    "dispatch/screens/browser.py",
    '''        self._size_worker_generation = 0
        self._size_column_key: ColumnKey | None = None
''',
    '''        self._size_worker_generation = 0
        self._sel_column_key: ColumnKey | None = None
        self._size_column_key: ColumnKey | None = None
''',
)
replace(
    "dispatch/screens/browser.py",
    '''    async def on_mount(self) -> None:
        table = self.query_one("#browser-table", BrowserTable)
        # Name then Size: analysts scan names first; Size stays immediately to the right.
        table.add_column("Sel", width=_SEL_COLUMN_WIDTH)
''',
    '''    def on_mount(self) -> None:
        table = self.query_one("#browser-table", BrowserTable)
        # Name then Size: analysts scan names first; Size stays immediately to the right.
        self._sel_column_key = table.add_column("Sel", width=_SEL_COLUMN_WIDTH)
''',
)
replace(
    "dispatch/screens/browser.py",
    '''        self._show_detail_placeholder()
        self._update_action_state()
        if self._auto_load:
            await self.action_show_tables()
''',
    '''        self._show_detail_placeholder()
        self._update_action_state()
        if self._auto_load:
            # Paint the Browser shell first. SHOW TABLES / DESCRIBE may take
            # hundreds of milliseconds on the Edge Node and must not delay the
            # first interactive frame over SSH.
            self.run_worker(
                self.action_show_tables(),
                name="browser-initial-load",
                group="browser-load",
                exclusive=True,
            )
''',
)
replace(
    "dispatch/screens/browser.py",
    '''    def _update_size_cell(self, name: str, size_display: str) -> None:
        table = self.query_one("#browser-table", DataTable)
        if self._size_column_key is None:
            return
        try:
            # Keep the fixed Size width from _sync_column_widths; auto-growing
            # here reintroduces horizontal overflow on narrow SSH terminals.
            table.update_cell(name, self._size_column_key, size_display, update_width=False)
        except CellDoesNotExist:
            pass
''',
    '''    def _update_size_cell(self, name: str, size_display: str) -> None:
        table = self.query_one("#browser-table", DataTable)
        if self._size_column_key is None:
            return
        try:
            # Keep the fixed Size width from _sync_column_widths; auto-growing
            # here reintroduces horizontal overflow on narrow SSH terminals.
            table.update_cell(name, self._size_column_key, size_display, update_width=False)
        except CellDoesNotExist:
            pass

    def _update_check_cell(self, name: str) -> None:
        """Patch one selection marker without repainting the whole table."""
        table = self.query_one("#browser-table", DataTable)
        if self._sel_column_key is None:
            return
        try:
            table.update_cell(
                name,
                self._sel_column_key,
                self._check_marker(name),
                update_width=False,
            )
        except CellDoesNotExist:
            pass

    def _update_all_check_cells(self) -> None:
        """Patch selection markers in place, preserving cursor and scroll."""
        for name in self._tables:
            self._update_check_cell(name)
''',
)
replace(
    "dispatch/screens/browser.py",
    '''        if name in self._checked:
            self._checked.remove(name)
        else:
            self._checked.add(name)
        self._render_table_list(selected_before=name)
        self._update_action_state()
''',
    '''        if name in self._checked:
            self._checked.remove(name)
        else:
            self._checked.add(name)
        self._update_check_cell(name)
        self._update_action_state()
''',
)
replace(
    "dispatch/screens/browser.py",
    '''        cursor_name = self._selected_table()
        self._render_table_list(selected_before=cursor_name if cursor_name in self._tables else "")
        self._update_action_state()
''',
    '''        self._update_all_check_cells()
        self._update_action_state()
''',
)

# Dashboard: do not make screen mount wait on the initial manifest/log snapshot.
replace("dispatch/screens/dashboard.py", "    async def on_mount(self) -> None:\n", "    def on_mount(self) -> None:\n")
replace(
    "dispatch/screens/dashboard.py",
    '''        if hasattr(type(self.app), "kerberos_ttl"):
            self.watch(self.app, "kerberos_ttl", self._on_kerberos_change, init=True)
        await self._refresh_jobs_async()
        self.set_interval(2.0, self._refresh_jobs_async)
        table.focus()
''',
    '''        if hasattr(type(self.app), "kerberos_ttl"):
            self.watch(self.app, "kerberos_ttl", self._on_kerberos_change, init=True)
        self.run_worker(
            self._refresh_jobs_async(),
            name="dashboard-initial-refresh",
            group="dashboard-refresh",
            exclusive=True,
        )
        self.set_interval(2.0, self._refresh_jobs_async)
        table.focus()
''',
)
replace(
    "dispatch/screens/dashboard.py",
    '''            self._apply_jobs_snapshot(
                {
''',
    '''            if self.app.screen is not self:
                return
            self._apply_jobs_snapshot(
                {
''',
)

# History: paint first, then scan old manifests off the event loop.
replace(
    "dispatch/screens/history.py",
    '''        self._page = 0
        self._filtered: list[dict] = []
''',
    '''        self._page = 0
        self._all_jobs: list[dict] = []
        self._filtered: list[dict] = []
''',
)
replace("dispatch/screens/history.py", "    async def on_mount(self) -> None:\n", "    def on_mount(self) -> None:\n")
replace(
    "dispatch/screens/history.py",
    '''        table.cursor_type = "row"
        self.query_one("#history-empty").display = False
        self._all_jobs = await asyncio.to_thread(jobs.history_jobs)
        self._filtered = self._all_jobs
        self._render_history()
        if self._filtered:
            table.focus()
        else:
            self.query_one("#search", Input).focus()
''',
    '''        table.cursor_type = "row"
        self.query_one("#history-empty").display = False
        self.query_one("#history-status", Static).update("Loading history…")
        # Mount first; old-history scans can touch many manifests on the Edge
        # filesystem and should not delay the first interactive frame.
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
''',
)

# Job Detail: paint before first disk snapshot and coalesce expensive RichLog
# recoloring while the user types a search query.
replace(
    "dispatch/screens/job_detail.py",
    '''LOG_READ_CHUNK_BYTES = 65536

# Most recent shell/query attempts shown in the compact history line; older
''',
    '''LOG_READ_CHUNK_BYTES = 65536

# Search text itself paints immediately, but recoloring the complete 200-line
# RichLog window is coalesced so a burst of SSH keystrokes causes one repaint.
LOG_SEARCH_DEBOUNCE_SECONDS = 0.15

# Most recent shell/query attempts shown in the compact history line; older
''',
)
replace(
    "dispatch/screens/job_detail.py",
    '''        self._detail_timer: Timer | None = None
        self._monitor_timer: Timer | None = None
''',
    '''        self._detail_timer: Timer | None = None
        self._monitor_timer: Timer | None = None
        self._search_rebuild_timer: Timer | None = None
''',
)
replace("dispatch/screens/job_detail.py", "    async def on_mount(self) -> None:\n", "    def on_mount(self) -> None:\n")
replace(
    "dispatch/screens/job_detail.py",
    '''        if self.cancel_on_mount:
            self.action_cancel()
        await self._refresh_detail_async()
        self._detail_timer = self.set_interval(1.0, self._refresh_detail_async)
''',
    '''        if self.cancel_on_mount:
            self.action_cancel()
        # Do not make the screen mount wait for manifest/log I/O. The shell is
        # immediately usable while the first snapshot arrives in a worker.
        self.run_worker(
            self._refresh_detail_async(),
            name=f"detail-initial-{self.job_id}",
            group="detail-initial",
            exclusive=True,
        )
        self._detail_timer = self.set_interval(1.0, self._refresh_detail_async)
''',
)
replace(
    "dispatch/screens/job_detail.py",
    '''        for timer in (self._detail_timer, self._monitor_timer):
''',
    '''        for timer in (self._detail_timer, self._monitor_timer, self._search_rebuild_timer):
''',
)
replace(
    "dispatch/screens/job_detail.py",
    '''        self._detail_timer = None
        self._monitor_timer = None
''',
    '''        self._detail_timer = None
        self._monitor_timer = None
        self._search_rebuild_timer = None
''',
)
replace(
    "dispatch/screens/job_detail.py",
    '''    def action_log_search(self) -> None:
        search = self.query_one("#log-search-input", Input)
        search.display = not search.display
        if search.display:
            search.focus()
        else:
            self._search_query = ""
            search.value = ""
            self._rebuild_log()
            self.query_one("#log-display", RichLog).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "log-search-input":
            self._search_query = event.value.strip()
            self._rebuild_log()
''',
    '''    def action_log_search(self) -> None:
        search = self.query_one("#log-search-input", Input)
        search.display = not search.display
        if search.display:
            search.focus()
        else:
            if self._search_rebuild_timer is not None:
                self._search_rebuild_timer.stop()
                self._search_rebuild_timer = None
            self._search_query = ""
            with self.prevent(Input.Changed):
                search.value = ""
            self._rebuild_log()
            self.query_one("#log-display", RichLog).focus()

    def _schedule_log_rebuild(self) -> None:
        if self._search_rebuild_timer is not None:
            self._search_rebuild_timer.stop()
        self._search_rebuild_timer = self.set_timer(
            LOG_SEARCH_DEBOUNCE_SECONDS,
            self._run_scheduled_log_rebuild,
        )

    def _run_scheduled_log_rebuild(self) -> None:
        self._search_rebuild_timer = None
        self._rebuild_log()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "log-search-input":
            self._search_query = event.value.strip()
            self._schedule_log_rebuild()
''',
)

# New Job: all path stat/read + SQL analysis work is debounced and moved into
# one latest-value worker. The synchronous input path reads only widget state and
# cached validation state.
replace(
    "dispatch/screens/new_job.py",
    '''        self._sql_analysis_cache: tuple[tuple[str, str, str], AnalysisResult] | None = None
        self._validation_summary_timer: Timer | None = None
''',
    '''        self._sql_analysis_cache: tuple[tuple[str, str, str], AnalysisResult] | None = None
        self._validation_summary_timer: Timer | None = None
        self._live_validation_generation = 0
''',
)
replace(
    "dispatch/screens/new_job.py",
    '''        if hasattr(type(self.app), "kerberos_ttl"):
            self.watch(self.app, "kerberos_ttl", self._on_kerberos_change, init=True)
        self._detect_sql()
        self._update_field_visibility()
        self._inline_validate()
        self._update_validation_summary()
''',
    '''        if hasattr(type(self.app), "kerberos_ttl"):
            self.watch(self.app, "kerberos_ttl", self._on_kerberos_change, init=True)
        self._update_field_visibility()
        self._inline_validate()
        self.query_one("#validation-summary", Static).update("[dim]Checking…[/]")
        self._schedule_validation_summary(delay=0.0)
''',
)
replace(
    "dispatch/screens/new_job.py",
    '''    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._update_field_visibility()
        self._inline_validate()
        self._update_validation_summary()
''',
    '''    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._update_field_visibility()
        self._inline_validate()
        self._schedule_validation_summary()
''',
)
replace(
    "dispatch/screens/new_job.py",
    '''    def _inline_validate(self) -> None:
        """Provide real-time field validation indicators."""
        source = self._selected_source()
        msgs = []
        if source in ("SqlFile", "SqlTemplate"):
            if self._sql_file_exists():
                msgs.append("[green]\\u2713[/] SQL file found")
            elif self._input_value("sql-file"):
                msgs.append("[red]\\u2717[/] SQL file not found")
''',
    '''    def _inline_validate(self) -> None:
        """Paint cheap validation only; filesystem checks arrive asynchronously."""
        source = self._selected_source()
        msgs = []
        if source in ("SqlFile", "SqlTemplate"):
            raw = self._input_value("sql-file")
            cached = self._sql_exists_cache
            if cached is not None and cached[0] == raw:
                if cached[1]:
                    msgs.append("[green]\\u2713[/] SQL file found")
                elif raw:
                    msgs.append("[red]\\u2717[/] SQL file not found")
            elif raw:
                msgs.append("[dim]\\u2026 SQL file checking[/]")
''',
)
replace(
    "dispatch/screens/new_job.py",
    '''    def _schedule_validation_summary(self) -> None:
        if self._validation_summary_timer is not None:
            self._validation_summary_timer.stop()
        self._validation_summary_timer = self.set_timer(0.2, self._run_scheduled_validation_summary)

    def _run_scheduled_validation_summary(self) -> None:
        self._validation_summary_timer = None
        self._update_validation_summary()
''',
    '''    def _schedule_validation_summary(self, *, delay: float = 0.15) -> None:
        """Coalesce live validation and run all file/SQL work off the UI loop."""
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
        def compute() -> tuple[
            list[str], AnalysisResult, bool | None, AnalysisResult, str | None
        ]:
            issues: list[str] = []
            if inputs.source_type == "ExistingTable" and custom_schema_choice == "other":
                schema_error = sql.validate_identifier(custom_schema, "Schema")
                if schema_error:
                    issues.append(schema_error)
            shared = job_ops.validation_issues(inputs, kerberos_ttl=ttl, deep=False)
            for issue in shared:
                if issue == job_ops.MSG_KERBEROS_MISSING:
                    issues.append("Kerberos ticket missing \\u2014 press K to kinit")
                elif issue == job_ops.MSG_KERBEROS_TTL_SHORT:
                    issues.append("Kerberos ticket TTL is under 5 minutes \\u2014 press K to renew")
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
                        sql_text,
                        source_type=inputs.source_type,
                        user_id=inputs.user,
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
            # Source detection changes the legal form matrix. Let the ensuing
            # RadioSet event schedule a fresh validation for that source rather
            # than briefly painting a summary computed under the old source.
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
            summary.update(f"[red]\\u2717 {len(issues)} issue(s): {first}{extra}[/]  \\u00b7  {badge}")
        else:
            summary.update(f"[green]\\u2713 Ready to launch[/]  \\u00b7  {badge}")
''',
)
replace(
    "dispatch/screens/new_job.py",
    '''    def _refresh_path_hint(self) -> None:
        """Update the path hint to show just the filename of the SQL file."""
        raw = self._input_value("sql-file")
        hint = self.query_one("#path-hint", Static)
        if raw:
            name = Path(raw).name
            exists = self._sql_file_exists()
            icon = "[green]\\u2713[/]" if exists else "[red]\\u2717[/]"
            hint.update(f"{icon} [dim]{name}[/]")
        else:
            hint.update("")
''',
    '''    def _refresh_path_hint(self) -> None:
        """Update the path hint without touching the filesystem on the UI path."""
        raw = self._input_value("sql-file")
        hint = self.query_one("#path-hint", Static)
        if raw:
            name = Path(raw).name
            cached = self._sql_exists_cache
            if cached is not None and cached[0] == raw:
                icon = "[green]\\u2713[/]" if cached[1] else "[red]\\u2717[/]"
            else:
                icon = "[dim]\\u2026[/]"
            hint.update(f"{icon} [dim]{name}[/]")
        else:
            hint.update("")
''',
)
replace(
    "dispatch/screens/new_job.py",
    '''        launch_btn.disabled = (
            self.kerberos_ttl is None or self.kerberos_ttl < kerberos.MIN_LAUNCH_TTL_SECONDS
        )
        self._update_validation_summary()

    def _on_kerberos_change(self, value: int | None) -> None:
        self.kerberos_ttl = value
        self._refresh_kerberos()
        self._inline_validate()
        self._update_validation_summary()
''',
    '''        launch_btn.disabled = (
            self.kerberos_ttl is None or self.kerberos_ttl < kerberos.MIN_LAUNCH_TTL_SECONDS
        )
        self._schedule_validation_summary()

    def _on_kerberos_change(self, value: int | None) -> None:
        self.kerberos_ttl = value
        self._refresh_kerberos()
        self._inline_validate()
''',
)
replace(
    "dispatch/screens/new_job.py",
    '''    def _detect_sql(self) -> None:
        content = self._read_sql()
        if content is None:
            return
        detected = sql.detect_source(content)
        info = self.query_one("#info-detected", Static)
        info.update(
            f"Detected source: [b]{manifest.source_display_label(detected)}[/] "
            "\\u00b7 illegal destinations are disabled automatically"
        )
        if detected == "SqlTemplate":
            self.query_one("#src-sqltemplate", RadioButton).value = True
        elif detected == "ExistingTable":
            self.query_one("#src-existingtable", RadioButton).value = True
''',
    '''    def _apply_detected_source(self, detected: str) -> None:
        """Apply source detection from the debounced validation worker."""
        info = self.query_one("#info-detected", Static)
        info.update(
            f"Detected source: [b]{manifest.source_display_label(detected)}[/] "
            "\\u00b7 illegal destinations are disabled automatically"
        )
        if detected == "SqlTemplate":
            self.query_one("#src-sqltemplate", RadioButton).value = True
        elif detected == "ExistingTable":
            self.query_one("#src-existingtable", RadioButton).value = True
''',
)
replace(
    "dispatch/screens/new_job.py",
    '''        self._sql_exists_cache = None  # the editor may have created the file
        self._sql_analysis_cache = None  # or changed the SQL under the same path
        self._detect_sql()
''',
    '''        self._sql_exists_cache = None  # the editor may have created the file
        self._sql_analysis_cache = None  # or changed the SQL under the same path
        self._inline_validate()
        self._schedule_validation_summary(delay=0.0)
''',
)
replace(
    "dispatch/screens/new_job.py",
    '''        self._update_field_visibility()
        self._inline_validate()
        self._update_validation_summary()
''',
    '''        self._update_field_visibility()
        self._inline_validate()
        self._schedule_validation_summary()
''',
)

print("core performance transformations applied")

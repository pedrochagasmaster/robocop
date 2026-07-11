"""Job detail and live-tail screen."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static
from textual.worker import Worker

from .. import config, errors, jobs, manifest, process, telemetry
from ..formatting import (
    format_elapsed,
    format_job_id,
    format_state,
    format_timestamp,
    style_log_line,
)
from .confirm import ConfirmScreen
from .sidebar import Sidebar

# The live log view keeps only a bounded tail in memory and on screen so very
# chatty jobs cannot grow the UI without limit. The RichLog widget and the
# in-memory deque share this window so the truncation hint stays truthful.
LOG_VIEW_LINES = 200

# Maximum bytes read per 1s refresh tick. A chatty orchestrator can append
# more than this between ticks; the remainder is picked up on the next tick
# by carrying the offset forward. Bounds memory and RichLog write bursts.
LOG_READ_CHUNK_BYTES = 65536


class JobDetailScreen(Screen[None]):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("c", "cancel", "Cancel Job"),
        ("escape", "app.pop_screen", "Back"),
        ("space", "toggle_follow", "Follow"),
        Binding("f", "toggle_follow", "Follow", show=False),
        Binding("g", "log_top", "Log Top", show=False),
        Binding("G", "log_bottom", "Log Bottom", show=False),
        ("/", "log_search", "Search"),
        ("y", "copy_job_id", "Copy ID"),
        ("r", "clone_job", "Clone"),
    ]

    follow_mode = reactive(True)

    def __init__(self, job_id: str, cancel_on_mount: bool = False) -> None:
        super().__init__()
        self.job_id = job_id
        self.cancel_on_mount = cancel_on_mount
        self._tail_offset = 0
        self._tail_lines: deque[str] = deque(maxlen=LOG_VIEW_LINES)
        self._tail_pending_bytes = b""
        self._evicted_line_count = 0
        self._search_query = ""
        self._error_code: str | None = None
        self._error_line = ""
        # Error classification reads the log tail; do it once per failure,
        # not on every 1s refresh tick.
        self._error_checked = False
        self._job_state: str | None = None
        # Manifest mtime cache: skip the JSON parse when the file is unchanged.
        self._manifest_mtime: float | None = None
        self._manifest_item: dict[str, Any] | None = None
        self._refresh_in_flight = False
        # Last markup painted per Static, to skip no-op repaints over SSH.
        self._static_cache: dict[str, str] = {}

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide actions that do not apply to the Job's current state."""
        if action == "cancel":
            return self._job_state in (None, "Running", "Pending")
        if action == "clone_job":
            return self._job_state in (None, "Succeeded", "Failed", "Cancelled")
        return True

    @property
    def job_dir(self):
        return config.jobs_dir() / self.job_id

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        sidebar = Sidebar()
        sidebar.active_screen = "view_logs"
        yield sidebar
        with Vertical(id="main-content"):
            with Vertical(id="job-detail-content"):
                yield Static(
                    f"[dim]\u2039 Overview /[/] [bold]Job {format_job_id(self.job_id, 'full')}[/]",
                    classes="section-title",
                )

                with Vertical(id="job-summary-panel"):
                    with Horizontal(id="summary-grid"):
                        with Vertical():
                            yield Static("--", id="sum-state")
                            yield Static("--", id="sum-source")
                            yield Static("--", id="sum-dest")
                        with Vertical():
                            yield Static("--", id="sum-started")
                            yield Static("--", id="sum-elapsed")
                            yield Static("--", id="sum-csv")

                yield Static("", id="error-banner")

                with Horizontal(id="log-header"):
                    yield Static("[bold]Logs[/]", classes="section-title")
                    yield Static("", id="log-streaming")

                yield Static("", id="truncation-hint")

                with Vertical(id="log-panel"):
                    yield RichLog(
                        id="log-display",
                        highlight=True,
                        markup=True,
                        max_lines=LOG_VIEW_LINES,
                    )
                    yield Input(placeholder="Search log\u2026", id="log-search-input")

            with Horizontal(classes="action-bar"):
                yield Static("", id="job-status-line", classes="action-status")
                yield Button("Back [B]", id="back", variant="default")
                yield Button("Clone [R]", id="clone", variant="default")
                yield Button("Cancel Job [C]", id="cancel", variant="error")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#log-search-input").display = False
        self.query_one("#truncation-hint").display = False
        self.query_one("#error-banner").display = False
        self.query_one("#clone", Button).display = False
        if self.cancel_on_mount:
            self.action_cancel()
        await self._refresh_detail_async()
        self.set_interval(1.0, self._refresh_detail_async)

    async def _refresh_detail_async(self) -> None:
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        manifest_path = self.job_dir / "manifest.json"
        log_path = self.job_dir / "run.log"
        cached_manifest_mtime = self._manifest_mtime
        cached_manifest_item = self._manifest_item
        cached_error_code = self._error_code
        cached_error_line = self._error_line
        error_checked = self._error_checked

        def _read() -> dict[str, Any] | None:
            try:
                manifest_mtime = manifest_path.stat().st_mtime
            except OSError:
                return None
            if manifest_mtime == cached_manifest_mtime and cached_manifest_item is not None:
                item = cached_manifest_item
            else:
                try:
                    item = manifest.load(manifest_path)
                except Exception:
                    return None
            new_lines: list[str] = []
            try:
                size = log_path.stat().st_size
            except OSError:
                size = self._tail_offset
            offset = self._tail_offset
            # The log shrank: it was rotated or truncated. Re-read from the
            # start and signal the UI to drop the now-stale buffered lines so
            # they are not duplicated below the fresh content.
            reset = size < offset
            if reset:
                offset = 0
            pending_bytes = b"" if reset else self._tail_pending_bytes
            chunk = b""
            if size > offset:
                with log_path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read(LOG_READ_CHUNK_BYTES)
                    new_offset = handle.tell()
            else:
                new_offset = offset
            complete_lines = (pending_bytes + chunk).split(b"\n")
            pending_bytes = complete_lines.pop()
            # A live Job may append to an unterminated final line, so keep it
            # buffered. Terminal Jobs will not append again; flush their final
            # unterminated line exactly once when the reader reaches EOF.
            if pending_bytes and new_offset == size and item["state"] not in ("Running", "Pending"):
                complete_lines.append(pending_bytes)
                pending_bytes = b""
            new_lines = [line.decode("utf-8", errors="replace").rstrip() for line in complete_lines]
            error_code = cached_error_code
            error_line = cached_error_line
            if item["state"] == "Failed" and not error_checked:
                error_code = errors.classify(log_path)
                error_line = errors.first_matching_line(log_path, error_code)
            return {
                "item": item,
                "manifest_mtime": manifest_mtime,
                "new_lines": new_lines,
                "new_offset": new_offset,
                "pending_bytes": pending_bytes,
                "error_code": error_code,
                "error_line": error_line,
                "reset": reset,
            }

        try:
            snapshot = await asyncio.to_thread(_read)
            self._apply_detail_snapshot(snapshot)
        finally:
            self._refresh_in_flight = False

    def _set_static(self, selector: str, markup: str) -> None:
        """Update a Static only when its content actually changed."""
        if self._static_cache.get(selector) == markup:
            return
        self._static_cache[selector] = markup
        self.query_one(selector, Static).update(markup)

    def _apply_detail_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        if snapshot is None:
            return
        item = snapshot["item"]
        self._manifest_mtime = snapshot["manifest_mtime"]
        self._manifest_item = item
        self._error_code = snapshot["error_code"]
        self._error_line = snapshot["error_line"]
        dest = item["destination"]
        source = item["source"]
        state = item["state"]
        if state == "Failed":
            self._error_checked = True

        if state != self._job_state:
            self._job_state = state
            self.refresh_bindings()

        source_text = self._truncate_path(
            source.get("table_name") or source.get("sql_path_at_launch") or source.get("type", "--")
        )
        self._set_static("#sum-source", f"[dim]Source[/]       {source_text}")
        schema = dest.get("schema", "")
        table = dest.get("table_name", "")
        full_table = f"{schema}.{table}" if schema and table else dest.get("type", "--")
        self._set_static(
            "#sum-dest",
            f"[dim]Destination[/]  {dest.get('type', '--')} \u2192 {full_table}",
        )

        self._set_static(
            "#sum-state", f"[dim]State[/]        {format_state(state, self._error_code)}"
        )
        if state == "Running":
            self._update_streaming_indicator()
        else:
            streaming = {
                "Succeeded": "[dim]Complete[/]",
                "Failed": "[red]Failed[/]",
                "Cancelled": "[dim]Cancelled[/]",
            }.get(state, "")
            self._set_static("#log-streaming", streaming)

        self._set_static(
            "#sum-started", f"[dim]Started[/]  {format_timestamp(item.get('started_at'))}"
        )
        self._set_static("#sum-elapsed", f"[dim]Elapsed[/]  {format_elapsed(item)}")
        csv_path = dest.get("csv_path") or ""
        if dest.get("type") in ("Csv", "Table+Csv") and csv_path:
            csv_text = self._truncate_path(csv_path)
        else:
            csv_text = "[dim]n/a (table-only)[/]"
        self._set_static("#sum-csv", f"[dim]CSV[/]      {csv_text}")

        cancel_btn = self.query_one("#cancel", Button)
        cancel_btn.display = state in ("Running", "Pending")
        clone_btn = self.query_one("#clone", Button)
        clone_btn.display = state in ("Succeeded", "Failed", "Cancelled")

        self._update_error_banner(state)
        self._tail_pending_bytes = snapshot["pending_bytes"]
        self._append_log_lines(
            snapshot["new_lines"], snapshot["new_offset"], reset=snapshot.get("reset", False)
        )

        status_parts = [format_state(state, self._error_code)]
        if state == "Failed":
            status_parts.append(f"exit {item.get('exit_code', '?')}")
        if item.get("finished_at"):
            status_parts.append(f"finished {format_timestamp(item['finished_at'])}")
        self._set_static("#job-status-line", "  \u00b7  ".join(status_parts))

    def _update_streaming_indicator(self) -> None:
        if self.follow_mode:
            self._set_static(
                "#log-streaming", "[green]Streaming logs\u2026 (auto-scroll) \u25cf[/]"
            )
        else:
            self._set_static("#log-streaming", "[yellow][PAUSED][/]")

    def _update_error_banner(self, state: str) -> None:
        banner = self.query_one("#error-banner", Static)
        if state != "Failed":
            if banner.display:
                banner.display = False
            return
        code = self._error_code
        if code:
            self._set_static(
                "#error-banner",
                f"[bold red]{code}[/]: {self._error_line}\n[dim]{errors.suggestion(code)}[/]",
            )
        else:
            self._set_static("#error-banner", "[red]Job failed.[/] [dim]Check log for details.[/]")
        if not banner.display:
            banner.display = True

    def _styled_log_line(self, line: str) -> str:
        styled = self._style_log_line(line)
        if self._search_query and self._search_query.lower() in line.lower():
            return f"[reverse]{styled}[/]"
        return styled

    def _rebuild_log(self) -> None:
        """Repaint the whole visible window from ``_tail_lines``.

        Used when the search query changes (so already-visible lines pick up or
        drop highlight) rather than only styling freshly appended lines.
        """
        log_widget = self.query_one("#log-display", RichLog)
        log_widget.clear()
        for line in self._tail_lines:
            log_widget.write(self._styled_log_line(line))
        if self.follow_mode:
            log_widget.scroll_end(animate=False)

    def _append_log_lines(
        self, new_lines: list[str], new_offset: int, *, reset: bool = False
    ) -> None:
        log_widget = self.query_one("#log-display", RichLog)
        if reset:
            self._tail_lines.clear()
            self._evicted_line_count = 0
            log_widget.clear()
        elif not new_lines and new_offset == self._tail_offset:
            return
        for line in new_lines:
            before = len(self._tail_lines)
            self._tail_lines.append(line)
            if len(self._tail_lines) == before and before == self._tail_lines.maxlen:
                self._evicted_line_count += 1
            log_widget.write(self._styled_log_line(line))
        self._tail_offset = new_offset
        hint = self.query_one("#truncation-hint", Static)
        if self._evicted_line_count:
            hint.update(f"[dim][\u2026 {self._evicted_line_count} earlier lines not shown][/]")
            hint.display = True
        else:
            hint.display = False
        if self.follow_mode and new_lines:
            log_widget.scroll_end(animate=False)

    def watch_follow_mode(self, value: bool) -> None:
        self._update_streaming_indicator()

    def action_toggle_follow(self) -> None:
        self.follow_mode = not self.follow_mode
        if self.follow_mode:
            self.query_one("#log-display", RichLog).scroll_end(animate=False)

    def action_log_top(self) -> None:
        self.follow_mode = False
        self.query_one("#log-display", RichLog).scroll_home(animate=False)

    def action_log_bottom(self) -> None:
        self.follow_mode = True
        self.query_one("#log-display", RichLog).scroll_end(animate=False)

    def action_log_search(self) -> None:
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "log-search-input":
            self.action_log_search()

    def action_copy_job_id(self) -> None:
        try:
            self.app.copy_to_clipboard(self.job_id)
            self.notify("Job ID copied to clipboard", severity="information")
        except Exception:
            self.notify(self.job_id, title="Job ID", severity="information")

    def action_clone_job(self) -> None:
        from ..app import DispatchApp

        try:
            item = manifest.load(self.job_dir / "manifest.json")
        except Exception as exc:
            self.notify(f"Cannot clone: {exc}", severity="error")
            return
        cast_app = self.app
        if isinstance(cast_app, DispatchApp):
            cast_app.open_new_job_prefill(self._prefill_from_manifest(item))

    @staticmethod
    def _prefill_from_manifest(item: dict) -> dict:
        from .. import sql

        source = item.get("source", {})
        dest = item.get("destination", {})
        params = item.get("params", {}) or {}
        # Email, subject, and template dates live under ``params`` (dates as the
        # orchestrator's MM/DD/YYYY); convert dates back to the form's ISO input.
        return {
            "source_type": source.get("type", "SqlFile"),
            "sql_file": source.get("sql_path_at_launch", ""),
            "existing_table": source.get("table_name", ""),
            "schema": dest.get("schema", ""),
            "table_name": dest.get("table_name", ""),
            "dest_type": dest.get("type", "Table"),
            "email": params.get("to_email", ""),
            "subject": params.get("subject", "Dispatch Job"),
            "start_date": sql.from_orchestrator_date(params.get("start_date", "")),
            "end_date": sql.from_orchestrator_date(params.get("end_date", "")),
        }

    @staticmethod
    def _truncate_path(value: str, max_len: int = 40) -> str:
        if len(value) <= max_len:
            return value
        return f"\u2026{value[-max_len:]}"

    @staticmethod
    def _style_log_line(line: str) -> str:
        return style_log_line(line)

    def action_cancel(self) -> Worker[None]:
        """Run the confirm-and-cancel flow in a worker (see NewJobScreen.action_launch)."""
        return self.run_worker(self._cancel_flow(), name="cancel-flow", exclusive=True)

    async def _cancel_flow(self) -> None:
        try:
            item = manifest.load(self.job_dir / "manifest.json")
        except Exception:
            return
        pid = item.get("pid")
        if item["state"] == "Pending" and not pid:
            confirmed = await self._confirm_pending_cancel(item["id"])
            if not confirmed:
                return
            manifest.update(
                self.job_dir / "manifest.json",
                state="Cancelled",
                exit_code=0,
                finished_at=manifest.now_utc(),
            )
            telemetry.emit("job_cancelled", {"job_id": item["id"]})
            self.notify(f"Pending Job {item['id']} removed", severity="warning")
            self._set_static("#job-status-line", "[yellow]Pending Job cancelled[/]")
            return
        if item["state"] in ("Running", "Pending") and pid:
            confirmed = await self._confirm_cancel(item["id"], pid)
            if not confirmed:
                return
            try:
                result = process.cancel_process_group(pid)
            except ProcessLookupError:
                result = "missing"
            except PermissionError:
                self.notify(
                    "Permission denied while signalling the Job process group",
                    severity="error",
                )
                return
            if result == "missing":
                jobs.reconcile_manifest(self.job_dir / "manifest.json")
                self.notify(
                    "Job process is no longer running; manifest marked Failed",
                    severity="warning",
                )
                self._set_static("#job-status-line", "[red]Process missing; marked Failed[/]")
                return
            telemetry.emit("job_cancelled", {"job_id": item["id"]})
            self.notify(f"Cancellation requested for Job {item['id']}", severity="warning")
            self._set_static("#job-status-line", "[yellow]Cancellation requested\u2026[/]")
            return
        self.notify("No cancellable Job process found", severity="warning")

    async def _confirm_cancel(self, job_id: str, pid: int) -> bool:
        loop_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def on_result(result: bool | None) -> None:
            if not loop_future.done():
                loop_future.set_result(bool(result))

        self.app.push_screen(
            ConfirmScreen(
                "Cancel Job",
                (
                    f"Cancel Job [cyan]{job_id}[/]?\n\n"
                    f"This sends SIGTERM to process group PID [bold]{pid}[/]."
                ),
                danger=True,
                confirm_label="Cancel Job",
                cancel_label="Keep Running",
            ),
            callback=on_result,
        )
        return await loop_future

    async def _confirm_pending_cancel(self, job_id: str) -> bool:
        loop_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def on_result(result: bool | None) -> None:
            if not loop_future.done():
                loop_future.set_result(bool(result))

        self.app.push_screen(
            ConfirmScreen(
                "Remove Pending Job",
                (
                    f"Remove Pending Job [cyan]{job_id}[/]?\n\n"
                    "No runner PID has been recorded yet. The manifest will be "
                    "kept for audit history and marked Cancelled."
                ),
                danger=True,
                confirm_label="Remove Pending Job",
                cancel_label="Keep Pending",
            ),
            callback=on_result,
        )
        return await loop_future

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "clone":
            self.action_clone_job()

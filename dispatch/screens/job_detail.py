"""Job detail and live-tail screen."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Button, Footer, Header, Input, RichLog, Static
from textual.worker import Worker

from .. import config, errors, job_ops, manifest
from ..formatting import (
    format_elapsed,
    format_job_id,
    format_state,
    format_timestamp,
    style_log_line,
)
from ..monitor_service import MonitorSnapshot
from .confirm import ConfirmScreen
from .sidebar import Sidebar

if TYPE_CHECKING:
    from ..monitor_service import MonitorService, QueryAttempt

# The live log view keeps only a bounded tail in memory and on screen so very
# chatty jobs cannot grow the UI without limit. The RichLog widget and the
# in-memory deque share this window so the truncation hint stays truthful.
LOG_VIEW_LINES = 200

# Maximum bytes read per 1s refresh tick. A chatty orchestrator can append
# more than this between ticks; the remainder is picked up on the next tick
# by carrying the offset forward. Bounds memory and RichLog write bursts.
LOG_READ_CHUNK_BYTES = 65536

# Most recent shell/query attempts shown in the compact history line; older
# attempts are summarized by count rather than dropped silently.
MONITOR_HISTORY_LIMIT = 5

_PHASE_LABELS: dict[str, str] = {
    "preparing": "preparing",
    "queued": "queued",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "retrying": "retrying",
    "unknown": "unknown",
}


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
        ("m", "recover_identity", "Recover"),
    ]

    follow_mode = reactive(True)

    def __init__(
        self,
        job_id: str,
        cancel_on_mount: bool = False,
        *,
        monitor_service: MonitorService | None = None,
    ) -> None:
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
        # Slice 5 monitoring panel. ``None`` (the default) keeps monitoring
        # off entirely so every existing caller/test is unaffected; the app
        # wires its owned MonitorService through for real navigation.
        self._monitor_service = monitor_service
        self._monitor_subscribed = False
        self._monitor_refresh_in_flight = False
        self._monitor_generation = 0
        self._detail_timer: Timer | None = None
        self._monitor_timer: Timer | None = None
        self._compact_layout = False
        self._recovery_call_id: str | None = None
        self._recovery_checked_call_id: str | None = None
        self._recovery_in_flight = False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide actions that do not apply to the Job's current state."""
        if action == "cancel":
            return self._job_state in (None, "Running", "Pending")
        if action == "clone_job":
            return self._job_state in (None, "Succeeded", "Failed", "Cancelled")
        if action == "recover_identity":
            return self._recovery_call_id is not None and not self._recovery_in_flight
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

                with Vertical(id="monitor-panel"):
                    yield Static("", id="monitor-attempt")
                    yield Static("", id="monitor-history")

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
                yield Button("Recover [M]", id="recover-monitor", variant="default")
                yield Button("Cancel Job [C]", id="cancel", variant="error")
        yield Footer()

    async def on_mount(self) -> None:
        self._monitor_generation += 1
        self.query_one("#log-search-input").display = False
        self.query_one("#truncation-hint").display = False
        self.query_one("#error-banner").display = False
        self.query_one("#clone", Button).display = False
        self.query_one("#recover-monitor", Button).display = False
        self._update_layout_mode()
        if self.cancel_on_mount:
            self.action_cancel()
        await self._refresh_detail_async()
        self._detail_timer = self.set_interval(1.0, self._refresh_detail_async)
        self.app.run_worker(
            self._start_monitoring_async(),
            name=f"monitor-start-{self.job_id}",
            exclusive=False,
        )

    async def _start_monitoring_async(self) -> None:
        # Let the mount message finish so the ownership guard can require
        # ``app.screen is self`` without treating a normal mount as stale.
        await asyncio.sleep(0)
        if not self._monitor_result_is_current(self._monitor_generation):
            return
        await self._subscribe_monitor_async()
        if self.is_mounted and self.app.screen is self:
            self._monitor_timer = self.set_interval(2.0, self._refresh_monitor_async)

    def on_unmount(self) -> None:
        self._monitor_generation += 1
        for timer in (self._detail_timer, self._monitor_timer):
            if timer is not None:
                timer.stop()
        self._detail_timer = None
        self._monitor_timer = None
        # Screen is popped, not suspended-in-place (see app.py navigation);
        # dropping the subscription here returns this job's pollers to the
        # background cadence for every other consumer.
        if self._monitor_subscribed and self._monitor_service is not None:
            self._monitor_service.unsubscribe(self.job_id)
            self._monitor_subscribed = False

    def on_resize(self) -> None:
        self._update_layout_mode()

    def _update_layout_mode(self) -> None:
        compact = self.app.size.height < 30
        if compact == self._compact_layout:
            return
        self._compact_layout = compact
        self.set_class(compact, "compact")
        self.query_one("#monitor-history").display = not compact

    def _monitor_result_is_current(self, token: int) -> bool:
        return self.is_mounted and self.app.screen is self and token == self._monitor_generation

    def _show_monitor_error(self, token: int, exc: Exception) -> None:
        if not self._monitor_result_is_current(token):
            return
        self._set_static("#monitor-attempt", "[dim]Impala attempt: monitoring unavailable[/]")
        self._set_static("#monitor-history", "")
        self.notify(f"Monitoring unavailable: {type(exc).__name__}", severity="warning")

    def _unwired_monitor_snapshot(self) -> MonitorSnapshot:
        return MonitorSnapshot(
            job_id=self.job_id,
            available=False,
            unavailable_reason="monitoring unavailable",
        )

    async def _subscribe_monitor_async(self) -> None:
        """Subscribe to monitoring for this job, driving the foreground cadence.

        Paints the quiet "monitoring unavailable" line and returns without
        subscribing when no service was wired in — the default for every
        existing caller/test, and identical to a wired service reporting no
        sidecar for this job. Runs the (blocking, file-reading) ``subscribe``
        call in a thread so the event loop is never blocked, per the
        dispatch-textual-tui skill.
        """
        service = self._monitor_service
        if service is None:
            self._apply_monitor_snapshot(self._unwired_monitor_snapshot())
            return
        token = self._monitor_generation
        job_id = self.job_id
        job_dir = self.job_dir

        def _subscribe() -> MonitorSnapshot:
            return service.subscribe(job_id, job_dir)

        subscribe_task = asyncio.create_task(asyncio.to_thread(_subscribe))
        try:
            snapshot = await asyncio.shield(subscribe_task)
        except asyncio.CancelledError:
            # Cancelling the awaiting coroutine does not stop work already
            # running in ``to_thread``. Wait for that call to settle, then
            # compensate any successful foreground subscription before
            # propagating cancellation.
            try:
                await asyncio.shield(subscribe_task)
            except Exception:
                pass
            else:
                await asyncio.to_thread(service.unsubscribe, job_id)
            raise
        except Exception as exc:
            self._show_monitor_error(token, exc)
            return
        if not self._monitor_result_is_current(token):
            await asyncio.to_thread(service.unsubscribe, job_id)
            return
        self._monitor_subscribed = True
        self._apply_monitor_snapshot(snapshot)
        await self._check_recovery_eligibility(snapshot, token)

    async def _refresh_monitor_async(self) -> None:
        if self._monitor_service is None or self._monitor_refresh_in_flight:
            return
        self._monitor_refresh_in_flight = True
        service = self._monitor_service
        job_id = self.job_id
        token = self._monitor_generation

        def _snapshot() -> MonitorSnapshot:
            return service.snapshot(job_id)

        try:
            snapshot = await asyncio.to_thread(_snapshot)
            if self._monitor_result_is_current(token):
                self._apply_monitor_snapshot(snapshot)
                await self._check_recovery_eligibility(snapshot, token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._show_monitor_error(token, exc)
        finally:
            self._monitor_refresh_in_flight = False

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

    # -- Slice 5: Impala attempt monitoring panel -------------------------
    #
    # The manifest's coarse job state (rendered above, unchanged) is the only
    # authoritative job-level truth. This panel shows a *separate* signal:
    # the current Impala attempt's phase, pool, reported progress, queued
    # duration, and a compact shell/query attempt history, sourced only from
    # MonitorService snapshots. It never overrides or reinterprets the
    # manifest state, and a monitoring failure never touches cancel/log-tail
    # behavior above.

    def _apply_monitor_snapshot(self, snapshot: MonitorSnapshot) -> None:
        if not snapshot.available:
            reason = snapshot.unavailable_reason or "monitoring unavailable"
            self._set_static("#monitor-attempt", f"[dim]Impala attempt: {reason}[/]")
            self._set_static("#monitor-history", "")
            return

        leaf = self._current_leaf_attempt(snapshot)
        self._set_static("#monitor-attempt", self._format_current_attempt(leaf))
        self._set_static("#monitor-history", self._format_attempt_history(snapshot))

    @staticmethod
    def _recovery_candidate_call_id(snapshot: MonitorSnapshot) -> str | None:
        candidates = []
        for call in snapshot.orchestrator_calls:
            missing_shells = [shell for shell in call.shell_executions if not shell.queries]
            if missing_shells:
                candidates.append(call)
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        if candidate.index is None or candidate.call_id.startswith("legacy-"):
            return None
        return candidate.call_id

    async def _check_recovery_eligibility(self, snapshot: MonitorSnapshot, token: int) -> None:
        call_id = self._recovery_candidate_call_id(snapshot)
        if call_id is None:
            self._set_recovery_available(None)
            return
        if call_id == self._recovery_checked_call_id:
            return
        service = self._monitor_service
        if service is None:
            return
        self._recovery_checked_call_id = call_id
        try:
            await asyncio.to_thread(service.recovery_criteria, self.job_id, call_id)
        except Exception:
            if self._monitor_result_is_current(token):
                self._set_recovery_available(None)
            return
        if self._monitor_result_is_current(token):
            self._set_recovery_available(call_id)

    def _set_recovery_available(self, call_id: str | None) -> None:
        self._recovery_call_id = call_id
        self.query_one("#recover-monitor", Button).display = call_id is not None
        self.refresh_bindings()

    async def action_recover_identity(self) -> None:
        service = self._monitor_service
        call_id = self._recovery_call_id
        if service is None or call_id is None or self._recovery_in_flight:
            return
        token = self._monitor_generation
        self._recovery_in_flight = True
        self.refresh_bindings()

        def _recover() -> MonitorSnapshot:
            criteria = service.recovery_criteria(self.job_id, call_id)
            return service.recover_identity(
                self.job_id,
                call_id,
                criteria,
                seed_url=config.impala_monitor_seed_url(),
            )

        try:
            snapshot = await asyncio.to_thread(_recover)
            if self._monitor_result_is_current(token):
                self._apply_monitor_snapshot(snapshot)
                self._set_recovery_available(None)
                self.notify("Impala query identity recovered", severity="information")
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._monitor_result_is_current(token):
                self.notify("Identity unavailable/ambiguous", severity="warning")
        finally:
            self._recovery_in_flight = False
            if self._monitor_result_is_current(token):
                self.refresh_bindings()

    @staticmethod
    def _current_leaf_attempt(snapshot: MonitorSnapshot) -> QueryAttempt | None:
        """Return the most recently started live leaf query attempt, if any.

        A query superseded by a transparent retry is represented by its
        latest retry (the live continuation); the last shell in event order
        is the most current attempt.
        """
        for call in reversed(snapshot.orchestrator_calls):
            for shell in reversed(call.shell_executions):
                if not shell.queries:
                    continue
                query = shell.queries[-1]
                return query.latest_retry_leaf()
        return None

    def _format_current_attempt(self, leaf: QueryAttempt | None) -> str:
        if leaf is None:
            return "[dim]Impala attempt: no query observed yet[/]"
        observation = leaf.observation
        if observation is None:
            return "[dim]Impala attempt: awaiting first observation…[/]"

        if observation.availability_error:
            return (
                f"[dim]Impala attempt: monitoring unavailable ({observation.availability_error})[/]"
            )

        phase_text = _PHASE_LABELS.get(observation.phase, observation.phase)
        parts = [f"Impala attempt: [bold]{phase_text}[/]"]
        if observation.pool:
            parts.append(f"pool [cyan]{observation.pool}[/]")
        progress = observation.query_progress or observation.scan_progress
        if progress is not None and progress.display:
            parts.append(f"reported work completed: {progress.display}")
        if observation.queued_duration:
            parts.append(f"queued {observation.queued_duration}")
        return "  ·  ".join(parts)

    def _format_attempt_history(self, snapshot: MonitorSnapshot) -> str:
        entries: list[str] = []
        for call in snapshot.orchestrator_calls:
            shells = call.shell_executions
            for shell_index, shell in enumerate(shells):
                fallback_follows = (
                    shell_index + 1 < len(shells)
                    and shells[shell_index + 1].shell_relation == "orchestrator_pool_fallback"
                )
                for query_index, query in enumerate(shell.queries):
                    entries.extend(
                        self._describe_query_chain(
                            query,
                            pool=shell.pool,
                            fallback_follows=(
                                fallback_follows and query_index == len(shell.queries) - 1
                            ),
                        )
                    )
        if not entries:
            return ""
        shown = entries[-MONITOR_HISTORY_LIMIT:]
        lines = [f"[dim]Attempt history ({len(entries)} total):[/]"]
        lines.extend(f"  {entry}" for entry in shown)
        return "\n".join(lines)

    def _describe_query_chain(
        self, query: QueryAttempt, *, pool: str, fallback_follows: bool = False
    ) -> list[str]:
        """Describe one initial query and its transparent-retry chain.

        A mid-chain ``EXCEPTION`` (an attempt with at least one following
        retry) reads "attempt failed; job retrying" — never "job failed",
        since the manifest (not this panel) is the only source of job-level
        truth. Only the last attempt in the chain (no further retry) may
        report a terminal outcome as such.
        """
        chain = query.attempts_depth_first()
        described: list[str] = []
        for index, attempt in enumerate(chain):
            has_following = index < len(chain) - 1 or (index == len(chain) - 1 and fallback_follows)
            described.append(
                self._describe_single_attempt(attempt, pool=pool, has_following=has_following)
            )
        return described

    @staticmethod
    def _describe_single_attempt(query: QueryAttempt, *, pool: str, has_following: bool) -> str:
        observation = query.observation
        label = "retry" if query.relation == "transparent_retry" else "attempt"
        if observation is None:
            return f"{label} ({pool}): awaiting observation…"
        if observation.availability_error and observation.phase == "unknown":
            return f"{label} ({pool}): monitoring unavailable"
        phase_text = _PHASE_LABELS.get(observation.phase, observation.phase)
        if observation.phase == "failed" and has_following:
            return f"{label} ({pool}): attempt failed; job retrying"
        return f"{label} ({pool}): {phase_text}"

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
            "queue": params.get("queue", ""),
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
        elif item["state"] in ("Running", "Pending") and pid:
            confirmed = await self._confirm_cancel(item["id"], pid)
            if not confirmed:
                return
        else:
            self.notify("No cancellable Job process found", severity="warning")
            return

        try:
            result = job_ops.cancel_job(item["id"], yes=True)
        except job_ops.OperationalError as exc:
            self.notify(str(exc), severity="error")
            return
        except job_ops.UnknownJobError as exc:
            self.notify(str(exc), severity="error")
            return

        if result.kind == "pending_cancelled":
            self.notify(f"Pending Job {item['id']} removed", severity="warning")
            self._set_static("#job-status-line", "[yellow]Pending Job cancelled[/]")
            return
        if result.kind == "reconciled_missing":
            self.notify(
                "Job process is no longer running; manifest marked Failed",
                severity="warning",
            )
            self._set_static("#job-status-line", "[red]Process missing; marked Failed[/]")
            return
        if result.kind == "signaled":
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
        elif event.button.id == "recover-monitor":
            self.run_worker(self.action_recover_identity(), name="recover-monitor", exclusive=True)

"""UI snapshot and behavior tests for the Dispatch TUI.

Two types of tests live here:

``test_dashboard_snapshot``
    Structural snapshot: confirms the app renders without errors to a valid SVG
    and that key semantic strings (navigation labels, stat-card labels) are
    present.  The previous test hardcoded a ``viewBox`` pixel-geometry string
    which broke on any CSS or font change — that assertion is replaced by
    content-based checks.

``test_dashboard_shows_job_data``
    Pilot-driven behavior test: seeds a manifest on disk and confirms the
    dashboard table reflects the expected job state label.  Exercises the
    ``DashboardScreen.refresh_jobs`` path and the ``jobs.active_jobs`` seam at
    the widget level.

``TestJobDetailMonitorPanel``
    Slice 5 (``docs/research/impala-monitoring-implementation-plan.md``)
    coverage for the monitoring panel in ``JobDetailScreen``: unavailable,
    queued, running-with-progress, retry-chain, and
    attempt-failed-job-retrying states, driven by a fake ``MonitorService``
    exposing only the ``subscribe``/``unsubscribe``/``snapshot`` surface the
    screen depends on. Also covers that a monitoring failure leaves
    cancel/log-tail behavior untouched.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from textual.app import App
from textual.widgets import Button, Static

from dispatch import impala_monitor as im
from dispatch import jobs, manifest
from dispatch import monitor_service as ms
from dispatch.app import DispatchApp
from dispatch.screens.confirm import ConfirmScreen
from dispatch.screens.job_detail import JobDetailScreen

# ---------------------------------------------------------------------------
# Snapshot test — structural / content checks, no pixel geometry
# ---------------------------------------------------------------------------


def test_dashboard_snapshot(mock_env_with_config, tmp_path) -> None:
    """App renders to a valid SVG containing expected semantic content.

    Checks:
    - Output is a valid SVG document (starts with ``<svg``).
    - Key dashboard UI strings appear in the rendered output.
    - No ``Error:`` text indicating an unhandled render failure.

    The previous assertion ``viewBox="0 0 2946 1806.8"`` has been removed; it
    encoded Textual's internal pixel geometry and broke on any CSS or layout
    change without catching any real regression.
    """
    app = DispatchApp()
    out = tmp_path / "dashboard.svg"

    async def run() -> None:
        async with app.run_test(size=(240, 72)) as pilot:
            await pilot.pause(0.5)
            app.save_screenshot(filename=str(out))

    asyncio.run(run())

    assert out.exists(), "Screenshot file was not created"
    text = out.read_text(encoding="utf-8")

    assert text.startswith("<svg"), "Output is not a valid SVG document"
    assert "RUNNING" in text, "Stat card label 'RUNNING' missing from rendered output"
    assert "FINISHED" in text, "Stat card label 'FINISHED (7D)' missing"
    assert "KERBEROS" in text, "Stat card label 'KERBEROS' missing"
    assert "Error:" not in text, "Unexpected 'Error:' text in rendered output"


# ---------------------------------------------------------------------------
# Behavior test — dashboard renders job data from seeded manifests
# ---------------------------------------------------------------------------


def _seed_job(jobs_dir: Path, state: str, source_type: str = "SqlFile") -> manifest.JobManifest:
    """Write a minimal manifest to ``jobs_dir`` and return it."""
    from dispatch.manifest import now_utc

    job_id = f"20260516T10{len(list(jobs_dir.glob('*'))):04d}00Z_aaaaaa"
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True)
    finished = now_utc() if state != "Running" else None
    m: manifest.JobManifest = {
        "schema_version": 1,
        "id": job_id,
        "tool": "dispatch",
        "user": "testuser",
        "source": {"type": source_type},
        "destination": {"type": "Csv"},
        "params": {},
        "orchestrator_calls": [{"script": "download_to_csv.py", "argv": ["python3", "x.py"]}],
        "state": state,  # type: ignore[typeddict-item]
        "pid": None,
        "started_at": "2026-05-16T10:00:00Z",
        "finished_at": finished,
        "exit_code": 0 if state == "Succeeded" else (1 if state == "Failed" else None),
    }
    manifest.write(job_dir / "manifest.json", m)
    return m


def test_dashboard_shows_job_data(mock_env_with_config, tmp_path) -> None:
    """Dashboard renders state labels from seeded manifest data.

    Seeds one Succeeded and one Failed job, renders the dashboard to an SVG
    screenshot, and confirms that the state-label text appears in the output.
    This exercises the ``DashboardScreen.refresh_jobs`` → ``jobs.active_jobs``
    path end-to-end through the TUI renderer.
    """
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    _seed_job(jobs_dir, "Succeeded")
    _seed_job(jobs_dir, "Failed")

    app = DispatchApp()
    out = tmp_path / "dashboard_with_jobs.svg"

    async def run() -> None:
        async with app.run_test(size=(240, 72)) as pilot:
            # Allow DashboardScreen to mount and call refresh_jobs
            await pilot.pause(1.0)
            app.save_screenshot(filename=str(out))

    asyncio.run(run())

    assert out.exists()
    text = out.read_text(encoding="utf-8")

    # Both state labels from dashboard.py's _state_display logic must appear.
    # Textual strips markup (`[green]...[/]`) when rendering to the terminal,
    # so the plain words "SUCCEEDED" and "FAILED" appear in the SVG text nodes.
    assert "SUCCEEDED" in text, "Expected 'SUCCEEDED' job state label in dashboard screenshot"
    assert "FAILED" in text, "Expected 'FAILED' job state label in dashboard screenshot"


def test_confirm_screen_enter_confirms() -> None:
    """Enter follows the modal's advertised confirm shortcut."""

    class ConfirmTestApp(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.result: bool | None = None

        def on_mount(self) -> None:
            self.push_screen(
                ConfirmScreen("Launch job?", "Start the selected job?"),
                callback=self._capture_result,
            )

        def _capture_result(self, result: bool) -> None:
            self.result = result
            self.exit()

    async def run() -> bool | None:
        app = ConfirmTestApp()
        async with app.run_test() as pilot:
            await pilot.press("enter")
        return app.result

    assert asyncio.run(run()) is True


# ---------------------------------------------------------------------------
# Slice 5 — Job Detail monitoring panel
# ---------------------------------------------------------------------------

COORD_1 = "https://coordinator-1.internal.example:25443"
QID_1 = "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8"
QID_RETRY = "aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb"


def _seed_running_job(jobs_dir: Path, job_id: str = "20260716T100000Z_monitor") -> Path:
    """Seed a minimal Running-state manifest; returns the job directory.

    Used only to give ``JobDetailScreen`` a valid manifest to render its
    (unchanged) summary panel from — monitoring state itself always comes
    from the fake service, never from this manifest.
    """
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest.write(
        job_dir / "manifest.json",
        {
            "schema_version": 1,
            "id": job_id,
            "tool": "dispatch",
            "user": "testuser",
            "source": {"type": "SqlFile", "sql_path_at_launch": "/tmp/x.sql"},
            "destination": {"type": "Csv", "csv_path": "/tmp/x.csv"},
            "params": {},
            "orchestrator_calls": [{"script": "download_to_csv.py", "argv": ["python3", "x.py"]}],
            "state": "Running",  # type: ignore[typeddict-item]
            "pid": 4242,
            "started_at": "2026-07-16T10:00:00Z",
            "finished_at": None,
            "exit_code": None,
        },
    )
    return job_dir


def _observation(
    *,
    phase: im.Phase,
    raw_state: str | None,
    pool: str | None = "default",
    query_progress: im.ProgressCounter | None = None,
    scan_progress: im.ProgressCounter | None = None,
    queued_duration: str | None = None,
    availability_error: str | None = None,
) -> im.ImpalaObservation:
    return im.ImpalaObservation(
        raw_state=raw_state,
        phase=phase,
        pool=pool,
        scan_progress=scan_progress,
        query_progress=query_progress,
        queued_duration=queued_duration,
        bytes_read=None,
        rows_fetched=None,
        last_event=None,
        status_summary=None,
        detail_url=f"{COORD_1}/query_stmt?query_id={QID_1}&json",
        observed_at="2026-07-16T10:00:01Z",
        availability_error=availability_error,
    )


def _query(
    query_id: str,
    *,
    relation: im.Relation = "initial",
    observation: im.ImpalaObservation | None = None,
    retries: tuple[ms.QueryAttempt, ...] = (),
    seq: int = 1,
) -> ms.QueryAttempt:
    return ms.QueryAttempt(
        query_id=query_id,
        coordinator_base_url=COORD_1,
        relation=relation,
        shell_execution_id="shell-1",
        discovered_at="2026-07-16T10:00:00Z",
        seq=seq,
        retries=retries,
        observation=observation,
    )


class FakeMonitorService:
    """Minimal stand-in exposing only the surface ``JobDetailScreen`` uses.

    ``subscribe``/``unsubscribe``/``snapshot`` mirror
    ``dispatch.monitor_service.MonitorService``'s public signatures exactly,
    so swapping in the real service requires no screen changes. Snapshots are
    canned per job id via ``set_snapshot`` rather than computed from replayed
    events, keeping these tests focused on panel rendering.
    """

    def __init__(self) -> None:
        self.snapshots: dict[str, ms.MonitorSnapshot] = {}
        self.subscribe_calls: list[str] = []
        self.unsubscribe_calls: list[str] = []

    def set_snapshot(self, job_id: str, snapshot: ms.MonitorSnapshot) -> None:
        self.snapshots[job_id] = snapshot

    def subscribe(self, job_id: str, job_dir: Path) -> ms.MonitorSnapshot:
        self.subscribe_calls.append(job_id)
        return self.snapshot(job_id)

    def unsubscribe(self, job_id: str) -> None:
        self.unsubscribe_calls.append(job_id)

    def snapshot(self, job_id: str) -> ms.MonitorSnapshot:
        return self.snapshots.get(
            job_id,
            ms.MonitorSnapshot(
                job_id=job_id, available=False, unavailable_reason="monitoring unavailable"
            ),
        )


async def _mount_job_detail(
    app: DispatchApp, job_id: str, service: FakeMonitorService | None
) -> JobDetailScreen:
    screen = JobDetailScreen(job_id, monitor_service=service)  # type: ignore[arg-type]
    await app.push_screen(screen)
    return screen


class TestJobDetailMonitorPanel:
    def test_monitoring_unavailable_shows_quiet_line(self, mock_env_with_config) -> None:
        data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
        jobs_dir = data_root / ".dispatch" / "jobs"
        job_dir = _seed_running_job(jobs_dir)
        job_id = job_dir.name
        service = FakeMonitorService()
        # No snapshot registered: default is "monitoring unavailable".

        async def run() -> str:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _mount_job_detail(app, job_id, service)
                await pilot.pause(0.3)
                return str(screen.query_one("#monitor-attempt", Static).render())

        text = asyncio.run(run())
        assert "monitoring unavailable" in text.lower()

    def test_no_monitor_service_wired_behaves_like_before(self, mock_env_with_config) -> None:
        """Default ``monitor_service=None`` keeps every existing caller valid."""
        data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
        jobs_dir = data_root / ".dispatch" / "jobs"
        job_dir = _seed_running_job(jobs_dir)
        job_id = job_dir.name

        async def run() -> str:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _mount_job_detail(app, job_id, None)
                await pilot.pause(0.3)
                return str(screen.query_one("#monitor-attempt", Static).render())

        text = asyncio.run(run())
        assert "monitoring unavailable" in text.lower()

    def test_queued_state_shows_phase_pool_and_queued_duration(self, mock_env_with_config) -> None:
        data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
        jobs_dir = data_root / ".dispatch" / "jobs"
        job_dir = _seed_running_job(jobs_dir)
        job_id = job_dir.name
        service = FakeMonitorService()
        query = _query(
            QID_1,
            observation=_observation(
                phase="queued",
                raw_state="COMPILED",
                pool="adhoc_small",
                queued_duration="12s345ms",
            ),
        )
        shell = ms.ShellExecutionAttempt(
            shell_execution_id="shell-1", pool="adhoc_small", seq=1, queries=(query,)
        )
        service.set_snapshot(
            job_id,
            ms.MonitorSnapshot(
                job_id=job_id, available=True, unavailable_reason=None, shell_executions=(shell,)
            ),
        )

        async def run() -> str:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _mount_job_detail(app, job_id, service)
                await pilot.pause(0.3)
                return str(screen.query_one("#monitor-attempt", Static).render())

        text = asyncio.run(run())
        assert "queued" in text.lower()
        assert "adhoc_small" in text
        assert "12s345ms" in text
        # An ETA is never implied by this panel.
        assert "eta" not in text.lower()

    def test_running_state_shows_reported_work_completed_not_eta(
        self, mock_env_with_config
    ) -> None:
        data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
        jobs_dir = data_root / ".dispatch" / "jobs"
        job_dir = _seed_running_job(jobs_dir)
        job_id = job_dir.name
        service = FakeMonitorService()
        query = _query(
            QID_1,
            observation=_observation(
                phase="running",
                raw_state="RUNNING",
                pool="default",
                query_progress=im.ProgressCounter(completed=3, total=8, display="3 / 8"),
            ),
        )
        shell = ms.ShellExecutionAttempt(
            shell_execution_id="shell-1", pool="default", seq=1, queries=(query,)
        )
        service.set_snapshot(
            job_id,
            ms.MonitorSnapshot(
                job_id=job_id, available=True, unavailable_reason=None, shell_executions=(shell,)
            ),
        )

        async def run() -> str:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _mount_job_detail(app, job_id, service)
                await pilot.pause(0.3)
                return str(screen.query_one("#monitor-attempt", Static).render())

        text = asyncio.run(run())
        assert "running" in text.lower()
        assert "reported work completed" in text.lower()
        assert "3 / 8" in text
        assert "eta" not in text.lower()

    def test_retry_chain_shows_history_with_retry_entries(self, mock_env_with_config) -> None:
        data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
        jobs_dir = data_root / ".dispatch" / "jobs"
        job_dir = _seed_running_job(jobs_dir)
        job_id = job_dir.name
        service = FakeMonitorService()
        retry = _query(
            QID_RETRY,
            relation="transparent_retry",
            observation=_observation(phase="running", raw_state="RUNNING"),
            seq=2,
        )
        initial = _query(
            QID_1,
            observation=_observation(phase="failed", raw_state="EXCEPTION"),
            retries=(retry,),
        )
        shell = ms.ShellExecutionAttempt(
            shell_execution_id="shell-1", pool="default", seq=1, queries=(initial,)
        )
        service.set_snapshot(
            job_id,
            ms.MonitorSnapshot(
                job_id=job_id, available=True, unavailable_reason=None, shell_executions=(shell,)
            ),
        )

        async def run() -> tuple[str, str]:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _mount_job_detail(app, job_id, service)
                await pilot.pause(0.3)
                attempt_text = str(screen.query_one("#monitor-attempt", Static).render())
                history_text = str(screen.query_one("#monitor-history", Static).render())
                return attempt_text, history_text

        attempt_text, history_text = asyncio.run(run())
        # The live leaf is the retry, currently running.
        assert "running" in attempt_text.lower()
        # The history line must show the retry as its own entry.
        assert "retry" in history_text.lower()

    def test_mid_chain_exception_reads_attempt_failed_job_retrying(
        self, mock_env_with_config
    ) -> None:
        """A mid-chain EXCEPTION with a following attempt never reads 'job failed'."""
        data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
        jobs_dir = data_root / ".dispatch" / "jobs"
        job_dir = _seed_running_job(jobs_dir)
        job_id = job_dir.name
        service = FakeMonitorService()
        retry = _query(
            QID_RETRY,
            relation="transparent_retry",
            observation=_observation(phase="running", raw_state="RUNNING"),
            seq=2,
        )
        initial = _query(
            QID_1,
            observation=_observation(phase="failed", raw_state="EXCEPTION"),
            retries=(retry,),
        )
        shell = ms.ShellExecutionAttempt(
            shell_execution_id="shell-1", pool="default", seq=1, queries=(initial,)
        )
        service.set_snapshot(
            job_id,
            ms.MonitorSnapshot(
                job_id=job_id, available=True, unavailable_reason=None, shell_executions=(shell,)
            ),
        )

        async def run() -> str:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _mount_job_detail(app, job_id, service)
                await pilot.pause(0.3)
                return str(screen.query_one("#monitor-history", Static).render())

        history_text = asyncio.run(run())
        assert "attempt failed; job retrying" in history_text.lower()
        assert "job failed" not in history_text.lower()

    def test_subscribe_and_unsubscribe_called_on_mount_and_unmount(
        self, mock_env_with_config
    ) -> None:
        data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
        jobs_dir = data_root / ".dispatch" / "jobs"
        job_dir = _seed_running_job(jobs_dir)
        job_id = job_dir.name
        service = FakeMonitorService()

        async def run() -> FakeMonitorService:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await _mount_job_detail(app, job_id, service)
                await pilot.pause(0.3)
                assert service.subscribe_calls == [job_id]
                app.pop_screen()
                await pilot.pause(0.3)
            return service

        service = asyncio.run(run())
        assert service.unsubscribe_calls == [job_id]

    def test_monitoring_failure_leaves_cancel_and_log_tail_untouched(
        self, mock_env_with_config, monkeypatch
    ) -> None:
        """A monitoring-unavailable snapshot must not change cancel/log behavior.

        Seeds a Running job with a live pid so Cancel is available, appends a
        log line, and asserts both remain fully functional when the fake
        service reports "monitoring unavailable" throughout.
        """
        monkeypatch.setattr(jobs, "pid_is_alive", lambda pid: True)
        data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
        jobs_dir = data_root / ".dispatch" / "jobs"
        job_dir = _seed_running_job(jobs_dir)
        job_id = job_dir.name
        (job_dir / "run.log").write_text("hello from the orchestrator\n", encoding="utf-8")
        service = FakeMonitorService()  # never given a snapshot -> unavailable

        async def run() -> tuple[bool, str]:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _mount_job_detail(app, job_id, service)
                await pilot.pause(0.3)
                cancel_visible = screen.query_one("#cancel", Button).display
                from textual.widgets import RichLog

                log_widget = screen.query_one("#log-display", RichLog)
                log_text = "\n".join(str(line) for line in log_widget.lines)
                return cancel_visible, log_text

        cancel_visible, log_text = asyncio.run(run())
        assert cancel_visible is True
        assert "hello from the orchestrator" in log_text

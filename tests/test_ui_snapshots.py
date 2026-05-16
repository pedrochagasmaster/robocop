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
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dispatch.app import DispatchApp
from dispatch import manifest


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
    assert "SUCCEEDED" in text, (
        "Expected 'SUCCEEDED' job state label in dashboard screenshot"
    )
    assert "FAILED" in text, (
        "Expected 'FAILED' job state label in dashboard screenshot"
    )

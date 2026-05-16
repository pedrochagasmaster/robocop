"""Runner ↔ orchestrator integration tests.

ADR-0004: "the most interesting bugs live at the runner ↔ orchestrator
boundary, which unit tests can't cover."

Each test:
- Creates a real manifest on disk via ``manifest.create_job``.
- Spawns ``dispatch.runner`` as a subprocess (avoiding global-state pollution
  and signal-handler side-effects in the test process).
- Asserts the final manifest state written to disk by the runner.

The ``mock_env`` fixture from ``conftest.py`` ensures:
- ``mocks/bin/`` is prepended to ``PATH`` so the real orchestrators find the
  fake ``impala-shell``, ``klist``, and ``kinit``.
- ``DISPATCH_MOCK_STATE_DIR`` is a fresh ``tmp_path`` sub-directory, so the
  call-count state used by ``memory_exceeded`` never leaks between tests.
- ``DISPATCH_DATA_ROOT`` redirects all Job directories to a temp location.
- ``DISPATCH_SCR_DIR`` points at the real ``scr/`` directory so orchestrator
  imports resolve correctly.
- ``MAILHOST`` points at a closed port so email attempts fail fast.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dispatch import manifest


def _create_csv_job(tmp_path: Path, user: str = "testuser") -> tuple[Path, manifest.JobManifest]:
    """Create a minimal SqlFile → Csv job manifest and return (job_dir, manifest)."""
    sql_file = tmp_path / "test.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")
    return manifest.create_job(
        source={"type": "SqlFile", "sql_path_at_launch": str(sql_file)},
        destination={"type": "Csv", "table_name": "test_export", "schema": "", "csv_path": ""},
        params={"to_email": "test@example.com", "subject": "Test"},
        launch_cwd=tmp_path,
        sql_text="SELECT 1;",
        user=user,
    )


def _spawn_runner(job_dir: Path) -> subprocess.CompletedProcess:
    """Run dispatch.runner synchronously and return the completed process."""
    return subprocess.run(
        [sys.executable, "-m", "dispatch.runner", "--job-dir", str(job_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _read_log(job_dir: Path) -> str:
    log = job_dir / "run.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


# =============================================================================
# Lifecycle transitions per scenario
# =============================================================================

class TestRunnerLifecycle:

    def test_happy_path_reaches_succeeded(self, mock_env, tmp_path):
        """happy_path scenario: runner sets manifest state to Succeeded."""
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Succeeded", _read_log(job_dir)
        assert final["exit_code"] == 0
        assert final["finished_at"] is not None
        assert result.returncode == 0

    def test_syntax_error_reaches_failed(self, mock_env, monkeypatch, tmp_path):
        """syntax_error is a FATAL_ERRORS member → orchestrator exits 1 → Failed."""
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "syntax_error")
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Failed", _read_log(job_dir)
        assert final["exit_code"] != 0
        assert result.returncode != 0

    def test_auth_error_reaches_failed(self, mock_env, monkeypatch, tmp_path):
        """auth_error is a FATAL_ERRORS member → orchestrator exits 1 → Failed."""
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "auth_error")
        job_dir, _ = _create_csv_job(tmp_path)
        _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Failed", _read_log(job_dir)

    def test_table_not_found_reaches_failed(self, mock_env, monkeypatch, tmp_path):
        """table_not_found is TABLE_NOT_FOUND (FATAL) → Failed."""
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "table_not_found")
        job_dir, _ = _create_csv_job(tmp_path)
        _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Failed", _read_log(job_dir)

    def test_memory_exceeded_eventually_succeeds(self, mock_env, monkeypatch, tmp_path):
        """memory_exceeded fails twice then succeeds on 3rd pool → Succeeded."""
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "memory_exceeded")
        job_dir, _ = _create_csv_job(tmp_path)
        _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Succeeded", _read_log(job_dir)


# =============================================================================
# Manifest state guard (prevents double-spawn)
# =============================================================================

class TestRunnerStateGuard:

    def test_runner_exits_4_when_state_is_not_pending(self, mock_env, tmp_path):
        """Runner exits with code 4 if manifest.state != Pending."""
        job_dir, _ = _create_csv_job(tmp_path)
        # Manually transition to Running to simulate a double-spawn attempt
        manifest.update(job_dir / "manifest.json", state="Running")

        result = _spawn_runner(job_dir)
        assert result.returncode == 4

        # Manifest must not be mutated by the second spawn
        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Running"

    def test_runner_writes_error_file_on_bad_manifest(self, mock_env, tmp_path):
        """Corrupt manifest.json causes runner to write manifest.error.json and exit 3."""
        job_dir = Path(str(tmp_path)) / "badjob"
        job_dir.mkdir()
        (job_dir / "manifest.json").write_text("not valid json", encoding="utf-8")

        result = _spawn_runner(job_dir)
        assert result.returncode == 3
        assert (job_dir / "manifest.error.json").exists()


# =============================================================================
# Manifest state transitions during the run
# =============================================================================

class TestRunnerStateTransitions:

    def test_started_at_populated_after_run(self, mock_env, tmp_path):
        job_dir, _ = _create_csv_job(tmp_path)
        _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["started_at"] is not None

    def test_pid_populated_during_run_then_present_in_final(self, mock_env, tmp_path):
        job_dir, _ = _create_csv_job(tmp_path)
        _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["pid"] is not None

    def test_run_log_created_and_non_empty(self, mock_env, tmp_path):
        job_dir, _ = _create_csv_job(tmp_path)
        _spawn_runner(job_dir)

        log = job_dir / "run.log"
        assert log.exists()
        assert log.stat().st_size > 0


# =============================================================================
# Cancellation via SIGTERM
# =============================================================================

class TestRunnerCancellation:

    def test_sigterm_sets_state_to_cancelled(self, mock_env, tmp_path):
        """SIGTERM during an in-flight Job sets manifest.state to Cancelled.

        Uses a minimal fake orchestrator (a sleep script) so the test does not
        depend on the real orchestrators or mock impala-shell.  The purpose
        here is to verify the runner's SIGTERM → Cancelled path, not the full
        runner ↔ orchestrator integration.
        """
        # Fake orchestrator: write a marker then sleep indefinitely
        marker = tmp_path / "started.txt"
        fake_orch = tmp_path / "sleeping_orch.py"
        fake_orch.write_text(
            f"import time, pathlib\n"
            f"pathlib.Path({str(marker)!r}).touch()\n"
            f"time.sleep(300)\n"
        )

        # Build the manifest manually to point at the fake orchestrator
        job_dir, initial = _create_csv_job(tmp_path)
        import json
        m = json.loads((job_dir / "manifest.json").read_text())
        m["orchestrator_calls"] = [
            {"script": "fake", "argv": [sys.executable, str(fake_orch)]}
        ]
        (job_dir / "manifest.json").write_text(json.dumps(m, indent=2))

        proc = subprocess.Popen(
            [sys.executable, "-m", "dispatch.runner", "--job-dir", str(job_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait until the fake orchestrator has started (marker file created)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if marker.exists():
                break
            time.sleep(0.05)
        else:
            proc.kill()
            proc.wait()
            pytest.fail("Fake orchestrator did not start in time")

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("Runner did not exit after SIGTERM within 15s")

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Cancelled", _read_log(job_dir)

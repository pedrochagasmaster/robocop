"""Regression tests for stale Job reconciliation and explicit Pending cancel."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

from dispatch import jobs, manifest, process
from dispatch.app import DispatchApp
from dispatch.screens.job_detail import JobDetailScreen


def _create_csv_job(tmp_path: Path, *, user: str = "testuser") -> Path:
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")
    job_dir, _item = manifest.create_job(
        source={"type": "SqlFile", "sql_path_at_launch": str(sql_file)},
        destination={
            "type": "Csv",
            "schema": "",
            "table_name": "dispatch_export",
            "csv_path": "",
        },
        params={"to_email": "test@example.com", "subject": "Test"},
        launch_cwd=tmp_path,
        sql_text="SELECT 1;",
        user=user,
    )
    return job_dir


def test_stale_running_manifest_reconciles_to_failed(mock_env, tmp_path: Path, monkeypatch) -> None:
    job_dir = _create_csv_job(tmp_path)
    manifest_path = job_dir / "manifest.json"
    (job_dir / "run.log").write_text("started\n", encoding="utf-8")
    manifest.update(manifest_path, state="Running", pid=999_999, started_at=manifest.now_utc())
    monkeypatch.setattr(jobs, "pid_is_alive", lambda pid: False)

    reconciled = jobs.reconcile_manifest(manifest_path)

    assert reconciled is not None
    assert reconciled["state"] == "Failed"
    assert reconciled["exit_code"] == -1
    assert reconciled["finished_at"] is not None
    assert "stale runner pid 999999" in (job_dir / "run.log").read_text(encoding="utf-8")


def test_running_jobs_reconcile_dead_pid_before_counting(
    mock_env, tmp_path: Path, monkeypatch
) -> None:
    stale_dir = _create_csv_job(tmp_path)
    live_dir = _create_csv_job(tmp_path)
    manifest.update(
        stale_dir / "manifest.json",
        state="Running",
        pid=111_111,
        started_at=manifest.now_utc(),
    )
    manifest.update(
        live_dir / "manifest.json",
        state="Running",
        pid=222_222,
        started_at=manifest.now_utc(),
    )
    monkeypatch.setattr(jobs, "pid_is_alive", lambda pid: pid == 222_222)

    running = jobs.running_jobs()

    assert [item["pid"] for item in running] == [222_222]
    assert manifest.load(stale_dir / "manifest.json")["state"] == "Failed"
    assert jobs.can_launch() is True


def test_orphan_pending_without_pid_reconciles_after_grace(
    mock_env, tmp_path: Path, monkeypatch
) -> None:
    jobs._manifest_cache.clear()
    job_dir = _create_csv_job(tmp_path)
    manifest_path = job_dir / "manifest.json"
    (job_dir / "run.log").write_text("queued\n", encoding="utf-8")
    old_enough = manifest_path.stat().st_mtime - jobs.PENDING_ORPHAN_GRACE.total_seconds() - 5
    os.utime(manifest_path, (old_enough, old_enough))
    monkeypatch.setattr(jobs, "pid_is_alive", lambda pid: False)

    reconciled = jobs.reconcile_manifest(manifest_path)

    assert reconciled is not None
    assert reconciled["state"] == "Failed"
    assert reconciled["exit_code"] == -1
    assert reconciled["finished_at"] is not None
    assert "Pending job exceeded startup grace" in (job_dir / "run.log").read_text(encoding="utf-8")


def test_fresh_pending_without_pid_stays_pending(mock_env, tmp_path: Path) -> None:
    jobs._manifest_cache.clear()
    job_dir = _create_csv_job(tmp_path)
    manifest_path = job_dir / "manifest.json"

    reconciled = jobs.reconcile_manifest(manifest_path)

    assert reconciled is not None
    assert reconciled["state"] == "Pending"
    assert manifest.load(manifest_path)["state"] == "Pending"


def test_runner_transition_after_jobs_reconciliation_snapshot_is_preserved(
    mock_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    jobs._manifest_cache.clear()
    job_dir = _create_csv_job(tmp_path)
    manifest_path = job_dir / "manifest.json"
    old_enough = manifest_path.stat().st_mtime - jobs.PENDING_ORPHAN_GRACE.total_seconds() - 5
    os.utime(manifest_path, (old_enough, old_enough))
    snapshot_evaluated = threading.Event()
    allow_reconciliation_write = threading.Event()
    original_reconcile = jobs.job_lifecycle.reconcile

    def pause_after_pending_evaluation(*args, **kwargs):
        reconciliation = original_reconcile(*args, **kwargs)
        if args[0]["state"] == "Pending" and reconciliation is not None:
            snapshot_evaluated.set()
            assert allow_reconciliation_write.wait(2)
        return reconciliation

    monkeypatch.setattr(jobs.job_lifecycle, "reconcile", pause_after_pending_evaluation)
    monkeypatch.setattr(jobs, "pid_is_alive", lambda pid: pid == os.getpid())
    reconciled: list[manifest.JobManifest | None] = []
    failures: list[BaseException] = []

    def reconcile() -> None:
        try:
            reconciled.append(jobs.reconcile_manifest(manifest_path))
        except BaseException as exc:
            failures.append(exc)

    reconciliation = threading.Thread(target=reconcile)
    reconciliation.start()
    assert snapshot_evaluated.wait(2)
    manifest.update(
        manifest_path,
        state="Running",
        pid=os.getpid(),
        started_at=manifest.now_utc(),
    )
    allow_reconciliation_write.set()
    reconciliation.join(2)

    assert not reconciliation.is_alive()
    assert failures == []
    assert len(reconciled) == 1
    assert reconciled[0] is not None
    assert reconciled[0]["state"] == "Running"
    assert manifest.load(manifest_path)["state"] == "Running"


def test_orphan_pending_frees_launch_slot(mock_env, tmp_path: Path) -> None:
    jobs._manifest_cache.clear()
    fresh_dir = _create_csv_job(tmp_path)
    orphan_dir = _create_csv_job(tmp_path)
    orphan_manifest = orphan_dir / "manifest.json"
    old_enough = orphan_manifest.stat().st_mtime - jobs.PENDING_ORPHAN_GRACE.total_seconds() - 5
    os.utime(orphan_manifest, (old_enough, old_enough))

    assert manifest.load(fresh_dir / "manifest.json")["state"] == "Pending"
    assert manifest.load(orphan_manifest)["state"] == "Pending"
    assert jobs.can_launch() is True
    assert manifest.load(orphan_manifest)["state"] == "Failed"


def test_cancel_dead_pid_marks_running_job_failed(
    mock_env_with_config, tmp_path: Path, monkeypatch
) -> None:
    job_dir = _create_csv_job(tmp_path)
    manifest.update(
        job_dir / "manifest.json",
        state="Running",
        pid=123_456,
        started_at=manifest.now_utc(),
    )

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = JobDetailScreen(job_dir.name)
            app.push_screen(screen)
            await pilot.pause()
            monkeypatch.setattr(screen, "_confirm_cancel", _async_true)
            monkeypatch.setattr(
                process,
                "cancel_process_group",
                lambda pid: (_ for _ in ()).throw(ProcessLookupError(pid)),
            )
            monkeypatch.setattr(jobs, "pid_is_alive", lambda pid: False)

            await screen._cancel_flow()
            await pilot.pause()

    asyncio.run(run())

    final = manifest.load(job_dir / "manifest.json")
    assert final["state"] == "Failed"
    assert final["exit_code"] == -1


def test_pending_cancel_without_pid_marks_job_cancelled(
    mock_env_with_config, tmp_path: Path, monkeypatch
) -> None:
    job_dir = _create_csv_job(tmp_path)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = JobDetailScreen(job_dir.name)
            app.push_screen(screen)
            await pilot.pause()
            monkeypatch.setattr(screen, "_confirm_pending_cancel", _async_true)

            await screen._cancel_flow()
            await pilot.pause()

    asyncio.run(run())

    final = manifest.load(job_dir / "manifest.json")
    assert final["state"] == "Cancelled"
    assert final["exit_code"] == 0
    assert final["finished_at"] is not None
    assert job_dir.exists()


async def _async_true(*_args, **_kwargs) -> bool:
    return True

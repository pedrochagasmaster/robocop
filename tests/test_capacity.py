from __future__ import annotations

import multiprocessing
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from dispatch import manifest
from dispatch.capacity import (
    CapacityBusy,
    CapacityLedgerError,
    CapacityTimeout,
    admit_launch,
    try_acquire_metadata,
)

PROCESS_TIMEOUT = 10


def _job_manifest(job_id: str, state: str, pid: int | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": 1,
        "id": job_id,
        "tool": "dispatch",
        "user": "capacity-test",
        "source": {"type": "SqlFile", "sql_path_at_launch": "query.sql"},
        "destination": {"type": "Csv", "csv_path": "result.csv"},
        "params": {},
        "orchestrator_calls": [{"script": "test.py", "argv": ["test.py"]}],
        "state": state,
        "pid": pid,
        "started_at": now if state == "Running" else None,
        "finished_at": now if state not in {"Pending", "Running"} else None,
        "exit_code": 0 if state not in {"Pending", "Running"} else None,
    }


def _write_job(
    root: Path,
    job_id: str,
    state: str = "Pending",
    pid: int | None = None,
) -> Path:
    path = root / ".dispatch" / "jobs" / job_id / "manifest.json"
    manifest.write(path, _job_manifest(job_id, state, pid))  # type: ignore[arg-type]
    return path


def _join(process: multiprocessing.Process) -> None:
    process.join(PROCESS_TIMEOUT)
    assert not process.is_alive(), f"process {process.pid} did not exit"
    assert process.exitcode == 0


def _lease_worker(
    root: Path,
    release: Any,
    outcomes: Any,
) -> None:
    try:
        lease = try_acquire_metadata("describe", root)
    except CapacityBusy:
        outcomes.put(("busy", os.getpid()))
        return
    outcomes.put(("acquired", os.getpid()))
    release.wait(PROCESS_TIMEOUT)
    lease.release()


def _abandon_lease_worker(root: Path, acquired: Any) -> None:
    try_acquire_metadata("describe", root)
    acquired.set()
    os._exit(0)


def _held_lease_worker(root: Path, acquired: Any, release: Any) -> None:
    lease = try_acquire_metadata("describe", root)
    acquired.set()
    release.wait(PROCESS_TIMEOUT)
    lease.release()


def _launch_worker(
    root: Path,
    label: str,
    started: Any,
    callback_order: Any,
    outcomes: Any,
) -> None:
    started.set()

    def create_pending() -> str:
        callback_order.append(label)
        _write_job(root, label)
        return label

    try:
        result = admit_launch(create_pending, timeout=5, root=root)
    except Exception as exc:
        outcomes.put(("error", label, type(exc).__name__, str(exc)))
        return
    outcomes.put(("admitted", result))


def _queued_launch_without_job(root: Path, callback_started: Any, outcomes: Any) -> None:
    def callback() -> str:
        callback_started.set()
        return "launch"

    try:
        outcomes.put(("admitted", admit_launch(callback, timeout=5, root=root)))
    except Exception as exc:
        outcomes.put(("error", type(exc).__name__, str(exc)))


def _exit_cleanly() -> None:
    return


def test_processes_share_a_two_slot_metadata_limit(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    release = ctx.Event()
    outcomes = ctx.Queue()
    processes = [
        ctx.Process(target=_lease_worker, args=(tmp_path, release, outcomes)) for _ in range(3)
    ]

    for process in processes:
        process.start()
    observed = [outcomes.get(timeout=PROCESS_TIMEOUT)[0] for _ in processes]
    release.set()
    for process in processes:
        _join(process)

    assert sorted(observed) == ["acquired", "acquired", "busy"]


def test_dead_process_metadata_lease_is_reclaimed(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    process = ctx.Process(target=_abandon_lease_worker, args=(tmp_path, acquired))

    process.start()
    assert acquired.wait(PROCESS_TIMEOUT)
    process.join(PROCESS_TIMEOUT)
    assert process.exitcode == 0

    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("describe", tmp_path)
    with pytest.raises(CapacityBusy):
        try_acquire_metadata("describe", tmp_path)
    first.release()
    second.release()


def test_live_process_metadata_lease_is_not_reclaimed(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(target=_held_lease_worker, args=(tmp_path, acquired, release))
    process.start()
    assert acquired.wait(PROCESS_TIMEOUT)

    second = try_acquire_metadata("describe", tmp_path)
    with pytest.raises(CapacityBusy):
        try_acquire_metadata("describe", tmp_path)

    second.release()
    release.set()
    _join(process)


def test_launch_intents_are_admitted_fifo_across_processes(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    manager = ctx.Manager()
    callback_order = manager.list()
    outcomes = ctx.Queue()
    first_started = ctx.Event()
    second_started = ctx.Event()
    first_lease = try_acquire_metadata("describe", tmp_path)
    second_lease = try_acquire_metadata("describe", tmp_path)
    first = ctx.Process(
        target=_launch_worker,
        args=(tmp_path, "first", first_started, callback_order, outcomes),
    )
    second = ctx.Process(
        target=_launch_worker,
        args=(tmp_path, "second", second_started, callback_order, outcomes),
    )

    first.start()
    assert first_started.wait(PROCESS_TIMEOUT)
    time.sleep(0.2)
    second.start()
    assert second_started.wait(PROCESS_TIMEOUT)
    time.sleep(0.2)
    assert first.is_alive()
    assert second.is_alive()

    first_lease.release()
    second_lease.release()
    _join(first)
    _join(second)
    observed = [outcomes.get(timeout=PROCESS_TIMEOUT) for _ in range(2)]
    observed_order = list(callback_order)
    manager.shutdown()

    assert observed_order == ["first", "second"]
    assert sorted(observed) == [("admitted", "first"), ("admitted", "second")]


def test_waiting_launch_has_priority_over_new_stats_lease(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    callback_started = ctx.Event()
    outcomes = ctx.Queue()
    first_lease = try_acquire_metadata("describe", tmp_path)
    second_lease = try_acquire_metadata("describe", tmp_path)
    launch = ctx.Process(
        target=_queued_launch_without_job,
        args=(tmp_path, callback_started, outcomes),
    )
    launch.start()
    time.sleep(0.2)
    assert launch.is_alive()

    first_lease.release()
    try:
        stats_lease = try_acquire_metadata("stats", tmp_path)
    except CapacityBusy:
        stats_lease = None
    else:
        assert callback_started.is_set(), "a new stats lease overtook a queued launch"
        stats_lease.release()

    second_lease.release()
    _join(launch)
    assert outcomes.get(timeout=PROCESS_TIMEOUT) == ("admitted", "launch")


def test_launch_times_out_and_removes_its_intent(tmp_path: Path) -> None:
    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("describe", tmp_path)

    with pytest.raises(CapacityTimeout):
        admit_launch(lambda: "not-created", timeout=0.05, root=tmp_path)

    first.release()
    stats = try_acquire_metadata("stats", tmp_path)
    stats.release()
    second.release()


def test_two_active_jobs_reject_launch_without_waiting(tmp_path: Path) -> None:
    admit_launch(lambda: _write_job(tmp_path, "first"), root=tmp_path)
    admit_launch(lambda: _write_job(tmp_path, "second"), root=tmp_path)
    callback_called = False
    started = time.monotonic()

    def create_pending() -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(CapacityBusy):
        admit_launch(create_pending, timeout=2, root=tmp_path)

    assert not callback_called
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("final_state", ["Succeeded", "Failed", "Cancelled"])
def test_terminal_job_reservation_is_reclaimed(tmp_path: Path, final_state: str) -> None:
    path = admit_launch(lambda: _write_job(tmp_path, "job"), root=tmp_path)
    manifest.update(
        path,
        state=final_state,
        finished_at=manifest.now_utc(),
        exit_code=0,
    )

    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("describe", tmp_path)
    first.release()
    second.release()


def test_missing_job_manifest_reservation_is_reclaimed(tmp_path: Path) -> None:
    path = admit_launch(lambda: _write_job(tmp_path, "missing"), root=tmp_path)
    path.unlink()

    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("describe", tmp_path)
    first.release()
    second.release()


def test_expired_pending_job_is_failed_and_reclaimed(tmp_path: Path) -> None:
    path = admit_launch(lambda: _write_job(tmp_path, "expired"), root=tmp_path)
    stale = time.time() - 6 * 60
    os.utime(path, (stale, stale))

    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("describe", tmp_path)

    assert manifest.load(path)["state"] == "Failed"
    first.release()
    second.release()


def test_dead_running_job_is_failed_and_reclaimed(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    runner = ctx.Process(target=_exit_cleanly)
    runner.start()
    runner_pid = runner.pid
    _join(runner)
    assert runner_pid is not None
    path = admit_launch(
        lambda: _write_job(tmp_path, "dead-runner", "Running", runner_pid),
        root=tmp_path,
    )

    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("describe", tmp_path)

    assert manifest.load(path)["state"] == "Failed"
    first.release()
    second.release()


def test_live_running_job_reservation_is_not_reclaimed(tmp_path: Path) -> None:
    path = admit_launch(
        lambda: _write_job(tmp_path, "live-runner", "Running", os.getpid()),
        root=tmp_path,
    )

    lease = try_acquire_metadata("describe", tmp_path)
    with pytest.raises(CapacityBusy):
        try_acquire_metadata("describe", tmp_path)

    assert manifest.load(path)["state"] == "Running"
    lease.release()


def test_malformed_ledger_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / ".dispatch"
    home.mkdir()
    (home / "capacity.json").write_text("{not-json", encoding="utf-8")
    callback_called = False

    def create_pending() -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("describe", tmp_path)
    with pytest.raises(CapacityLedgerError):
        admit_launch(create_pending, timeout=0, root=tmp_path)
    assert not callback_called


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not reliable on Windows")
def test_capacity_files_are_private(tmp_path: Path) -> None:
    lease = try_acquire_metadata("describe", tmp_path)
    home = tmp_path / ".dispatch"

    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "capacity.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "capacity.lock").stat().st_mode) == 0o600
    lease.release()

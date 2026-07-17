from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from dispatch import capacity, job_lifecycle, manifest
from dispatch.capacity import (
    CapacityBusy,
    CapacityLedgerError,
    CapacityTimeout,
    admit_launch,
    try_acquire_metadata,
)

PROCESS_TIMEOUT = 10


def _read_ledger(root: Path) -> dict[str, Any]:
    return json.loads((root / ".dispatch" / "capacity.json").read_text(encoding="utf-8"))


def _wait_for_intent_pids(root: Path, expected: list[int]) -> None:
    deadline = time.monotonic() + PROCESS_TIMEOUT
    while time.monotonic() < deadline:
        try:
            observed = [intent["pid"] for intent in _read_ledger(root)["launch_intents"]]
        except (FileNotFoundError, json.JSONDecodeError):
            observed = []
        if observed == expected:
            return
        time.sleep(0.01)
    pytest.fail(f"launch intents never reached {expected!r}; observed {observed!r}")


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
    callback_order: Any,
    outcomes: Any,
) -> None:
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


def _blocked_launch_worker(root: Path) -> None:
    admit_launch(lambda: "launch", timeout=30, root=root)


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


def test_windows_pid_probe_does_not_call_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    class Kernel32:
        def __init__(self) -> None:
            self.closed: list[int] = []

        def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
            assert access == 0x1000
            assert inherit is False
            assert pid == 4242
            return 99

        def GetExitCodeProcess(self, handle: int, exit_code: Any) -> bool:
            assert handle == 99
            exit_code._obj.value = 259
            return True

        def CloseHandle(self, handle: int) -> bool:
            self.closed.append(handle)
            return True

    kernel32 = Kernel32()
    monkeypatch.setattr(job_lifecycle, "_WINDOWS", True)
    monkeypatch.setattr(job_lifecycle, "_WINDOWS_KERNEL32", kernel32)

    def destructive_probe(pid: int, signal: int) -> None:
        raise AssertionError(f"os.kill({pid}, {signal}) must not run on Windows")

    monkeypatch.setattr(job_lifecycle.os, "kill", destructive_probe)

    assert job_lifecycle.pid_is_alive(4242)
    assert kernel32.closed == [99]


def test_launch_intents_are_admitted_fifo_across_processes(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    manager = ctx.Manager()
    callback_order = manager.list()
    outcomes = ctx.Queue()
    first_lease = try_acquire_metadata("describe", tmp_path)
    second_lease = try_acquire_metadata("describe", tmp_path)
    first = ctx.Process(
        target=_launch_worker,
        args=(tmp_path, "first", callback_order, outcomes),
    )
    second = ctx.Process(
        target=_launch_worker,
        args=(tmp_path, "second", callback_order, outcomes),
    )

    first.start()
    assert first.pid is not None
    _wait_for_intent_pids(tmp_path, [first.pid])
    second.start()
    assert second.pid is not None
    _wait_for_intent_pids(tmp_path, [first.pid, second.pid])

    first_lease.release()
    second_lease.release()
    _join(first)
    _join(second)
    observed = [outcomes.get(timeout=PROCESS_TIMEOUT) for _ in range(2)]
    observed_order = list(callback_order)
    manager.shutdown()

    assert observed_order == ["first", "second"]
    assert sorted(observed) == [("admitted", "first"), ("admitted", "second")]


def test_waiting_launch_has_priority_over_new_stats_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_waiting = threading.Event()
    resume_launch = threading.Event()
    outcome: list[str] = []
    original_attempt = capacity._try_admit_registered_launch

    def pause_after_blocked_attempt(*args: Any, **kwargs: Any) -> tuple[bool, Any | None]:
        result = original_attempt(*args, **kwargs)
        if not result[0]:
            launch_waiting.set()
            assert resume_launch.wait(PROCESS_TIMEOUT)
        return result

    monkeypatch.setattr(capacity, "_try_admit_registered_launch", pause_after_blocked_attempt)
    first_lease = try_acquire_metadata("describe", tmp_path)
    second_lease = try_acquire_metadata("describe", tmp_path)

    def launch_target() -> None:
        outcome.append(admit_launch(lambda: "launch", timeout=5, root=tmp_path))

    launch = threading.Thread(target=launch_target)
    launch.start()
    assert launch_waiting.wait(PROCESS_TIMEOUT)
    try:
        assert [intent["pid"] for intent in _read_ledger(tmp_path)["launch_intents"]] == [
            os.getpid()
        ]
        first_lease.release()
        with pytest.raises(CapacityBusy):
            try_acquire_metadata("stats", tmp_path)
        assert len(_read_ledger(tmp_path)["launch_intents"]) == 1
    finally:
        resume_launch.set()
        second_lease.release()
    launch.join(PROCESS_TIMEOUT)
    assert not launch.is_alive()
    assert outcome == ["launch"]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
def test_waiting_launch_removes_intent_on_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("describe", tmp_path)

    def interrupt_wait(delay: float) -> None:
        raise interruption()

    monkeypatch.setattr(capacity.time, "sleep", interrupt_wait)

    with pytest.raises(interruption):
        admit_launch(lambda: "not-created", timeout=5, root=tmp_path)

    assert _read_ledger(tmp_path)["launch_intents"] == []
    first.release()
    stats = try_acquire_metadata("stats", tmp_path)
    stats.release()
    second.release()


def test_callback_base_exception_removes_launch_intent(tmp_path: Path) -> None:
    def cancel_callback() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        admit_launch(cancel_callback, root=tmp_path)

    assert _read_ledger(tmp_path)["launch_intents"] == []


def test_dead_launch_intent_is_reclaimed(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("describe", tmp_path)
    launch = ctx.Process(target=_blocked_launch_worker, args=(tmp_path,))
    launch.start()
    assert launch.pid is not None
    _wait_for_intent_pids(tmp_path, [launch.pid])

    launch.terminate()
    launch.join(PROCESS_TIMEOUT)
    assert not launch.is_alive()

    first.release()
    stats = try_acquire_metadata("stats", tmp_path)
    assert _read_ledger(tmp_path)["launch_intents"] == []
    stats.release()
    second.release()


def test_launch_times_out_and_removes_its_intent(tmp_path: Path) -> None:
    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("describe", tmp_path)

    with pytest.raises(CapacityTimeout):
        admit_launch(lambda: "not-created", timeout=0.05, root=tmp_path)

    first.release()
    stats = try_acquire_metadata("stats", tmp_path)
    stats.release()
    second.release()


def test_zero_timeout_performs_one_immediate_admission_when_free(tmp_path: Path) -> None:
    callback_calls = 0

    def create_pending() -> str:
        nonlocal callback_calls
        callback_calls += 1
        return "created"

    assert admit_launch(create_pending, timeout=0, root=tmp_path) == "created"
    assert callback_calls == 1


def test_zero_timeout_fails_promptly_without_callback_when_busy(tmp_path: Path) -> None:
    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("show tables", tmp_path)
    callback_calls = 0

    def create_pending() -> str:
        nonlocal callback_calls
        callback_calls += 1
        return "created"

    started = time.monotonic()
    try:
        with pytest.raises(CapacityTimeout):
            admit_launch(create_pending, timeout=0, root=tmp_path)
    finally:
        first.release()
        second.release()

    assert time.monotonic() - started < 0.25
    assert callback_calls == 0
    assert _read_ledger(tmp_path)["launch_intents"] == []


def test_async_zero_timeout_performs_one_immediate_admission_when_free(
    tmp_path: Path,
) -> None:
    callback_calls = 0

    def create_pending() -> str:
        nonlocal callback_calls
        callback_calls += 1
        return "created"

    async def run() -> str:
        return await capacity.admit_launch_async(
            create_pending,
            timeout=0,
            root=tmp_path,
        )

    assert asyncio.run(run()) == "created"
    assert callback_calls == 1


def test_async_zero_timeout_fails_promptly_without_callback_when_busy(
    tmp_path: Path,
) -> None:
    first = try_acquire_metadata("describe", tmp_path)
    second = try_acquire_metadata("show tables", tmp_path)
    callback_calls = 0

    def create_pending() -> str:
        nonlocal callback_calls
        callback_calls += 1
        return "created"

    async def run() -> None:
        with pytest.raises(CapacityTimeout):
            await capacity.admit_launch_async(
                create_pending,
                timeout=0,
                root=tmp_path,
            )

    started = time.monotonic()
    try:
        asyncio.run(run())
    finally:
        first.release()
        second.release()

    assert time.monotonic() - started < 0.25
    assert callback_calls == 0
    assert _read_ledger(tmp_path)["launch_intents"] == []


def test_launch_deadline_includes_capacity_lock_acquisition(tmp_path: Path) -> None:
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    callback_called = False
    outcome: list[BaseException | str] = []

    def hold_lock() -> None:
        with capacity._locked_home(tmp_path):
            lock_acquired.set()
            assert release_lock.wait(PROCESS_TIMEOUT)

    def create_pending() -> str:
        nonlocal callback_called
        callback_called = True
        return "created"

    def launch() -> None:
        try:
            outcome.append(admit_launch(create_pending, timeout=0.05, root=tmp_path))
        except BaseException as exc:
            outcome.append(exc)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert lock_acquired.wait(PROCESS_TIMEOUT)
    waiter = threading.Thread(target=launch)
    waiter.start()
    time.sleep(0.1)
    release_lock.set()
    holder.join(PROCESS_TIMEOUT)
    waiter.join(PROCESS_TIMEOUT)

    assert not holder.is_alive()
    assert not waiter.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], CapacityTimeout)
    assert callback_called is False


def test_async_launch_deadline_includes_capacity_lock_acquisition(tmp_path: Path) -> None:
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    callback_called = False

    def hold_lock() -> None:
        with capacity._locked_home(tmp_path):
            lock_acquired.set()
            assert release_lock.wait(PROCESS_TIMEOUT)

    def create_pending() -> str:
        nonlocal callback_called
        callback_called = True
        return "created"

    async def run() -> None:
        with pytest.raises(CapacityTimeout):
            await capacity.admit_launch_async(
                create_pending,
                timeout=0.05,
                root=tmp_path,
            )

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert lock_acquired.wait(PROCESS_TIMEOUT)
    try:
        asyncio.run(run())
    finally:
        release_lock.set()
        holder.join(PROCESS_TIMEOUT)

    assert not holder.is_alive()
    assert callback_called is False


def test_launch_never_invokes_callback_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_load = capacity._load_reconciled
    callback_called = False

    def delayed_load(home: Path) -> tuple[Path, Any]:
        result = original_load(home)
        time.sleep(0.03)
        return result

    def create_pending() -> str:
        nonlocal callback_called
        callback_called = True
        return "created"

    monkeypatch.setattr(capacity, "_load_reconciled", delayed_load)

    with pytest.raises(CapacityTimeout):
        admit_launch(create_pending, timeout=0.01, root=tmp_path)

    assert callback_called is False


def test_two_active_jobs_reject_launch_without_waiting(tmp_path: Path) -> None:
    admit_launch(lambda: _write_job(tmp_path, "first"), root=tmp_path)
    admit_launch(lambda: _write_job(tmp_path, "second"), root=tmp_path)
    callback_called = False

    def create_pending() -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(CapacityBusy):
        admit_launch(create_pending, timeout=2, root=tmp_path)

    assert not callback_called


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


def test_runner_manifest_replacement_during_reconciliation_preserves_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = admit_launch(lambda: _write_job(tmp_path, "starting"), root=tmp_path)
    stale = time.time() - 6 * 60
    os.utime(path, (stale, stale))
    snapshot_loaded = threading.Event()
    runner_replaced_manifest = threading.Event()
    original_reconcile = job_lifecycle.reconcile

    def pause_after_snapshot(
        item: manifest.JobManifest,
        modified_at: datetime | None,
        **kwargs: Any,
    ) -> job_lifecycle.Reconciliation | None:
        snapshot_loaded.set()
        assert runner_replaced_manifest.wait(PROCESS_TIMEOUT)
        return original_reconcile(item, modified_at, **kwargs)

    monkeypatch.setattr(job_lifecycle, "reconcile", pause_after_snapshot)
    acquired: list[capacity.MetadataLease] = []
    failures: list[BaseException] = []

    def acquire_metadata() -> None:
        try:
            acquired.append(try_acquire_metadata("describe", tmp_path))
        except BaseException as exc:
            failures.append(exc)

    acquisition = threading.Thread(target=acquire_metadata)
    acquisition.start()
    assert snapshot_loaded.wait(PROCESS_TIMEOUT)
    manifest.update(
        path,
        state="Running",
        pid=os.getpid(),
        started_at=manifest.now_utc(),
    )
    runner_replaced_manifest.set()
    acquisition.join(PROCESS_TIMEOUT)

    assert not acquisition.is_alive()
    assert failures == []
    assert len(acquired) == 1
    assert manifest.load(path)["state"] == "Running"
    with pytest.raises(CapacityBusy):
        try_acquire_metadata("second", tmp_path)
    acquired[0].release()


def test_runner_manifest_write_after_snapshot_check_wins_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = admit_launch(lambda: _write_job(tmp_path, "check-write-race"), root=tmp_path)
    stale = time.time() - 6 * 60
    os.utime(path, (stale, stale))
    snapshot_compared = threading.Event()
    allow_reconcile_write = threading.Event()
    original_same_snapshot = capacity._same_manifest_snapshot

    def pause_after_successful_comparison(
        current: os.stat_result,
        loaded: os.stat_result,
    ) -> bool:
        same = original_same_snapshot(current, loaded)
        if same:
            snapshot_compared.set()
            assert allow_reconcile_write.wait(PROCESS_TIMEOUT)
        return same

    monkeypatch.setattr(capacity, "_same_manifest_snapshot", pause_after_successful_comparison)
    acquired: list[capacity.MetadataLease] = []
    failures: list[BaseException] = []

    def acquire_metadata() -> None:
        try:
            acquired.append(try_acquire_metadata("describe", tmp_path))
        except BaseException as exc:
            failures.append(exc)

    acquisition = threading.Thread(target=acquire_metadata)
    acquisition.start()
    assert snapshot_compared.wait(PROCESS_TIMEOUT)
    manifest.update(
        path,
        state="Running",
        pid=os.getpid(),
        started_at=manifest.now_utc(),
    )
    allow_reconcile_write.set()
    acquisition.join(PROCESS_TIMEOUT)

    assert not acquisition.is_alive()
    assert failures == []
    assert len(acquired) == 1
    assert manifest.load(path)["state"] == "Running"
    with pytest.raises(CapacityBusy):
        try_acquire_metadata("second", tmp_path)
    acquired[0].release()


def test_windows_manifest_lock_uses_same_byte_after_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions: list[tuple[int, int]] = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_descriptor: int, mode: int, _count: int) -> None:
            positions.append((handle.tell(), mode))

    monkeypatch.setattr(manifest, "fcntl", None)
    monkeypatch.setattr(manifest, "msvcrt", FakeMsvcrt, raising=False)
    with (tmp_path / "manifest.lock").open("w+b") as handle:
        handle.write(b"\0")
        manifest._lock_manifest(handle)
        handle.seek(1)
        manifest._unlock_manifest(handle)

    assert positions == [(0, FakeMsvcrt.LK_LOCK), (0, FakeMsvcrt.LK_UNLCK)]


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


def test_version_one_ledger_is_normalized_without_losing_valid_entries(
    tmp_path: Path,
) -> None:
    manifest_path = _write_job(tmp_path, "migrated")
    home = tmp_path / ".dispatch"
    legacy = {
        "version": 1,
        "next_sequence": 2,
        "metadata_owners": [
            {
                "token": "legacy-token",
                "pid": os.getpid(),
                "operation": "DESCRIBE",
                "created_at": manifest.now_utc(),
            }
        ],
        "launch_intents": [
            {
                "pid": os.getpid(),
                "sequence": 1,
                "created_at": manifest.now_utc(),
            }
        ],
        "job_reservations": [
            {
                "job_id": "migrated",
                "manifest_path": str(manifest_path),
            }
        ],
    }
    (home / "capacity.json").write_text(
        json.dumps(legacy),
        encoding="utf-8",
    )

    with pytest.raises(CapacityBusy):
        try_acquire_metadata("probe", tmp_path)

    normalized = _read_ledger(tmp_path)
    assert normalized["version"] == capacity.LEDGER_VERSION
    assert normalized["metadata_owners"] == legacy["metadata_owners"]
    assert normalized["job_reservations"] == legacy["job_reservations"]
    assert normalized["launch_intents"][0]["pid"] == os.getpid()
    assert normalized["launch_intents"][0]["sequence"] == 1
    assert normalized["launch_intents"][0]["deadline_at"] > time.time()


def test_malformed_version_one_ledger_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / ".dispatch"
    home.mkdir()
    malformed = {
        "version": 1,
        "next_sequence": 2,
        "metadata_owners": [],
        "launch_intents": [
            {
                "pid": os.getpid(),
                "sequence": 1,
            }
        ],
        "job_reservations": [],
    }
    (home / "capacity.json").write_text(
        json.dumps(malformed),
        encoding="utf-8",
    )

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("probe", tmp_path)


@pytest.mark.parametrize(
    "deadline_at",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_non_finite_launch_intent_deadline_is_rejected(
    tmp_path: Path,
    deadline_at: float,
) -> None:
    home = tmp_path / ".dispatch"
    home.mkdir()
    ledger = {
        "version": capacity.LEDGER_VERSION,
        "next_sequence": 2,
        "metadata_owners": [],
        "launch_intents": [
            {
                "pid": os.getpid(),
                "sequence": 1,
                "created_at": manifest.now_utc(),
                "deadline_at": deadline_at,
            }
        ],
        "job_reservations": [],
    }
    (home / "capacity.json").write_text(
        json.dumps(ledger),
        encoding="utf-8",
    )

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("probe", tmp_path)


def test_release_is_idempotent_and_token_scoped(tmp_path: Path) -> None:
    first = try_acquire_metadata("describe-first", tmp_path)
    second = try_acquire_metadata("describe-second", tmp_path)

    first.release()
    first.release()
    replacement = try_acquire_metadata("replacement", tmp_path)
    with pytest.raises(CapacityBusy):
        try_acquire_metadata("must-not-remove-second", tmp_path)

    owners = _read_ledger(tmp_path)["metadata_owners"]
    assert {owner["operation"] for owner in owners} == {"describe-second", "replacement"}
    replacement.release()
    second.release()


def test_ledger_replacement_fsyncs_containing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(capacity, "_fsync_directory", synced.append, raising=False)

    lease = try_acquire_metadata("describe", tmp_path)

    assert synced == [tmp_path / ".dispatch"]
    lease.release()


def test_unwritable_ledger_replacement_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capacity.time, "sleep", lambda delay: None)

    def deny_replace(source: Path, destination: Path) -> None:
        raise PermissionError("read-only capacity state")

    monkeypatch.setattr(capacity.os, "replace", deny_replace)

    with pytest.raises(CapacityLedgerError, match="cannot update capacity ledger"):
        try_acquire_metadata("describe", tmp_path)


@pytest.mark.parametrize("unsafe_name", ["capacity.json", "capacity.lock"])
def test_non_regular_capacity_file_fails_closed(tmp_path: Path, unsafe_name: str) -> None:
    home = tmp_path / ".dispatch"
    home.mkdir()
    (home / unsafe_name).mkdir()

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("describe", tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliable on Windows CI")
@pytest.mark.parametrize("unsafe_name", ["capacity.json", "capacity.lock"])
def test_symlinked_capacity_file_fails_closed(tmp_path: Path, unsafe_name: str) -> None:
    home = tmp_path / ".dispatch"
    home.mkdir()
    external = tmp_path / f"external-{unsafe_name}"
    external.write_text("{}", encoding="utf-8")
    (home / unsafe_name).symlink_to(external)

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("describe", tmp_path)


def test_non_directory_jobs_root_fails_closed(tmp_path: Path) -> None:
    lease = try_acquire_metadata("describe", tmp_path)
    lease.release()
    jobs = tmp_path / ".dispatch" / "jobs"
    jobs.write_text("not a directory", encoding="utf-8")

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("describe", tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliable on Windows CI")
def test_symlinked_jobs_root_fails_closed(tmp_path: Path) -> None:
    external_jobs = tmp_path / "external-jobs"
    external_jobs.mkdir()
    home = tmp_path / ".dispatch"
    home.mkdir()
    (home / "jobs").symlink_to(external_jobs, target_is_directory=True)

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("describe", tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliable on Windows CI")
def test_symlinked_job_directory_is_not_reconciled_outside_root(tmp_path: Path) -> None:
    external_job = tmp_path / "external-job"
    external_manifest = external_job / "manifest.json"
    manifest.write(
        external_manifest,
        _job_manifest("external", "Running", 999_999_999),  # type: ignore[arg-type]
    )
    jobs = tmp_path / ".dispatch" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "external").symlink_to(external_job, target_is_directory=True)

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("describe", tmp_path)

    assert manifest.load(external_manifest)["state"] == "Running"


def test_non_directory_job_entry_fails_closed(tmp_path: Path) -> None:
    jobs = tmp_path / ".dispatch" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "looks-like-a-job").write_text("not a directory", encoding="utf-8")

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("describe", tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliable on Windows CI")
def test_symlinked_manifest_is_not_reconciled_outside_root(tmp_path: Path) -> None:
    external_manifest = tmp_path / "external-manifest.json"
    manifest.write(
        external_manifest,
        _job_manifest("external", "Running", 999_999_999),  # type: ignore[arg-type]
    )
    job = tmp_path / ".dispatch" / "jobs" / "external"
    job.mkdir(parents=True)
    (job / "manifest.json").symlink_to(external_manifest)

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("describe", tmp_path)

    assert manifest.load(external_manifest)["state"] == "Running"


def test_non_regular_manifest_fails_closed(tmp_path: Path) -> None:
    manifest_path = tmp_path / ".dispatch" / "jobs" / "job" / "manifest.json"
    manifest_path.mkdir(parents=True)

    with pytest.raises(CapacityLedgerError):
        try_acquire_metadata("describe", tmp_path)


def test_exported_interface_is_explicit_and_small() -> None:
    assert capacity.__all__ == [
        "CapacityBusy",
        "CapacityLedgerError",
        "CapacityTimeout",
        "MetadataLease",
        "admit_launch",
        "admit_launch_async",
        "try_acquire_metadata",
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not reliable on Windows")
def test_capacity_files_are_private(tmp_path: Path) -> None:
    lease = try_acquire_metadata("describe", tmp_path)
    home = tmp_path / ".dispatch"

    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "capacity.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "capacity.lock").stat().st_mode) == 0o600
    lease.release()

"""Tests for the New Job execution-queue selection control (multi-select)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest
from textual.widgets import SelectionList

from dispatch import capacity, config, jobs, manifest
from dispatch.app import DispatchApp
from dispatch.screens.new_job import _QUEUE_AUTO, NewJobScreen


def _write_sql(data_root: Path) -> Path:
    sql_path = data_root / "queue.sql"
    sql_path.write_text("SELECT 1 AS smoke_check;\n", encoding="utf-8")
    return sql_path


def _launch_args(data_root: Path, label: str) -> dict[str, object]:
    sql_path = data_root / "queue.sql"
    if not sql_path.exists():
        _write_sql(data_root)
    return {
        "source": {"type": "SqlFile", "sql_path_at_launch": str(sql_path)},
        "destination": {
            "type": "Csv",
            "schema": "aa_enc",
            "table_name": label,
            "csv_path": str(data_root / f"{label}.csv"),
        },
        "params": {"to_email": "", "subject": label, "queue": "auto"},
        "launch_cwd": data_root,
        "sql_text": "SELECT 1 AS smoke_check;\n",
    }


def _intent_sequences(data_root: Path) -> list[int]:
    ledger = json.loads((data_root / ".dispatch" / "capacity.json").read_text(encoding="utf-8"))
    return [int(intent["sequence"]) for intent in ledger["launch_intents"]]


async def _wait_for_intent_count(data_root: Path, count: int) -> list[int]:
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        try:
            sequences = _intent_sequences(data_root)
        except (FileNotFoundError, json.JSONDecodeError):
            sequences = []
        if len(sequences) == count:
            return sequences
        await asyncio.sleep(0.01)
    pytest.fail(f"launch intent count never reached {count}")


def test_queue_defaults_to_auto_when_nothing_selected(mock_env_with_config) -> None:
    """A fresh form selects no queue; params carry the ``auto`` sentinel."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    _write_sql(data_root)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(app.launch_cwd))
            await pilot.pause(0.5)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            assert screen._selected_queues() == []
            assert screen._params()["queue"] == _QUEUE_AUTO

    asyncio.run(run())


def test_selecting_single_queue_flows_into_params(mock_env_with_config) -> None:
    """Choosing one queue is reflected in the launch params."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    _write_sql(data_root)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(app.launch_cwd))
            await pilot.pause(0.5)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            screen.query_one("#queue", SelectionList).select("acs_large")
            await pilot.pause(0.1)
            assert screen._selected_queues() == ["acs_large"]
            assert screen._params()["queue"] == "acs_large"

    asyncio.run(run())


def test_selecting_multiple_queues_serialises_in_priority_order(mock_env_with_config) -> None:
    """Multiple queues are allowed and normalised to display (priority) order."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    _write_sql(data_root)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(app.launch_cwd))
            await pilot.pause(0.5)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            selection = screen.query_one("#queue", SelectionList)
            # Toggle out of display order to prove deterministic normalisation.
            selection.select("acs_large")
            selection.select("adhoc_fast")
            await pilot.pause(0.1)
            assert screen._selected_queues() == ["adhoc_fast", "acs_large"]
            assert screen._params()["queue"] == "adhoc_fast,acs_large"

    asyncio.run(run())


def test_queue_selection_is_per_job_and_never_saved_as_default(mock_env_with_config) -> None:
    """Pinning a queue is a per-job act; it must not become a sticky default."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    _write_sql(data_root)
    # A legacy sticky default from an older build must also be ignored.
    config.save_form_defaults({"email": "a@b.com", "queue": "acs_large"})

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(app.launch_cwd))
            await pilot.pause(0.5)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            assert screen._selected_queues() == []

            screen.query_one("#queue", SelectionList).select("acs_large")
            await pilot.pause(0.1)
            screen._save_form_defaults()
            assert "queue" not in config.read_form_defaults()

    asyncio.run(run())


def test_prefill_restores_multiple_selected_queues(mock_env_with_config) -> None:
    """Re-running a job restores every queue it was launched with."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    sql_path = _write_sql(data_root)
    prefill = {
        "source_type": "SqlFile",
        "dest_type": "Csv",
        "sql_file": str(sql_path),
        "table_name": "queued_export",
        "queue": "adhoc_fast,acs_large",
    }

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(app.launch_cwd, prefill=prefill))
            await pilot.pause(0.5)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            assert screen._selected_queues() == ["adhoc_fast", "acs_large"]

    asyncio.run(run())


def test_new_job_validation_does_not_scan_manifests_for_capacity(
    mock_env_with_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    sql_path = _write_sql(data_root)

    async def fake_ttl() -> int:
        return 7200

    def unexpected_scan(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("Textual validation must not scan job manifests")

    monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = NewJobScreen(data_root, prefill={"sql_file": str(sql_path)})
            app.push_screen(screen)
            await pilot.pause(0.5)
            monkeypatch.setattr(jobs, "can_launch", unexpected_scan)
            screen._validation_issues()

    asyncio.run(run())


def test_launch_waits_behind_metadata_then_creates_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
    first = capacity.try_acquire_metadata("describe")
    second = capacity.try_acquire_metadata("show tables")

    async def run() -> tuple[Path, manifest.JobManifest]:
        task = asyncio.create_task(
            jobs.create_job_when_capacity_available(**_launch_args(tmp_path, "waited"), timeout=2)
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        assert list(config.jobs_dir().glob("*/manifest.json")) == []
        first.release()
        return await asyncio.wait_for(task, timeout=1)

    try:
        job_dir, item = asyncio.run(run())
    finally:
        first.release()
        second.release()

    assert item["state"] == "Pending"
    assert manifest.load(job_dir / "manifest.json")["state"] == "Pending"


def test_async_launch_path_preserves_fifo_intents_across_poll_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
    first_lease = capacity.try_acquire_metadata("describe")
    second_lease = capacity.try_acquire_metadata("show tables")
    callback_order: list[str] = []
    original_create = manifest.create_job

    def recording_create(**kwargs):
        callback_order.append(str(kwargs["params"]["subject"]))
        return original_create(**kwargs)

    monkeypatch.setattr(manifest, "create_job", recording_create)

    async def run() -> None:
        first = asyncio.create_task(
            jobs.create_job_when_capacity_available(
                **_launch_args(tmp_path, "fifo_first"), timeout=2
            )
        )
        await _wait_for_intent_count(tmp_path, 1)
        second = asyncio.create_task(
            jobs.create_job_when_capacity_available(
                **_launch_args(tmp_path, "fifo_second"), timeout=2
            )
        )
        initial_sequences = await _wait_for_intent_count(tmp_path, 2)
        try:
            await asyncio.sleep(0.35)
            assert _intent_sequences(tmp_path) == initial_sequences
            first_lease.release()
            second_lease.release()
            await asyncio.gather(first, second)
        finally:
            for task in (first, second):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        first_lease.release()
        second_lease.release()

    assert callback_order == ["fifo_first", "fifo_second"]


def test_new_job_ui_launches_wait_in_fifo_order(
    mock_env_with_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    sql_path = _write_sql(data_root)
    first_lease = capacity.try_acquire_metadata("describe")
    second_lease = capacity.try_acquire_metadata("show tables")
    launched_tables: list[str] = []

    async def fake_ttl() -> int:
        return 7200

    async def fake_launch_runner(job_dir: Path) -> int:
        item = manifest.load(job_dir / "manifest.json")
        launched_tables.append(item["destination"]["table_name"])
        return 0

    monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)
    monkeypatch.setattr("dispatch.process.launch_runner", fake_launch_runner)

    def prefill(table_name: str) -> dict[str, str]:
        return {
            "source_type": "SqlFile",
            "dest_type": "Csv",
            "sql_file": str(sql_path),
            "table_name": table_name,
        }

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            first_screen = NewJobScreen(data_root, prefill=prefill("ui_fifo_first"))
            app.push_screen(first_screen)
            await pilot.pause(0.5)
            monkeypatch.setattr(first_screen, "_confirm_launch", _confirm_launch)
            first = asyncio.create_task(first_screen._launch_flow())
            await _wait_for_intent_count(data_root, 1)

            second_screen = NewJobScreen(data_root, prefill=prefill("ui_fifo_second"))
            app.push_screen(second_screen)
            await pilot.pause(0.5)
            monkeypatch.setattr(second_screen, "_confirm_launch", _confirm_launch)
            second = asyncio.create_task(second_screen._launch_flow())
            await _wait_for_intent_count(data_root, 2)

            first_lease.release()
            second_lease.release()
            await asyncio.gather(first, second)

    try:
        asyncio.run(run())
    finally:
        first_lease.release()
        second_lease.release()

    assert launched_tables == ["ui_fifo_first", "ui_fifo_second"]


def test_two_active_jobs_reject_launch_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
    jobs.create_job_if_slot_available(**_launch_args(tmp_path, "first"), timeout=0)
    jobs.create_job_if_slot_available(**_launch_args(tmp_path, "second"), timeout=0)

    async def run() -> float:
        started = time.monotonic()
        with pytest.raises(capacity.CapacityBusy):
            await jobs.create_job_when_capacity_available(
                **_launch_args(tmp_path, "third"), timeout=2
            )
        return time.monotonic() - started

    elapsed = asyncio.run(run())

    assert elapsed < 0.25
    assert len(list(config.jobs_dir().glob("*/manifest.json"))) == 2


def test_cancelled_launch_removes_shared_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
    first = capacity.try_acquire_metadata("describe")
    second = capacity.try_acquire_metadata("show tables")

    async def run() -> None:
        task = asyncio.create_task(
            jobs.create_job_when_capacity_available(
                **_launch_args(tmp_path, "cancelled"), timeout=30
            )
        )
        await _wait_for_intent_count(tmp_path, 1)
        task.cancel()
        await asyncio.sleep(0)
        first.release()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
        first.release()
        stats = capacity.try_acquire_metadata("stats")
        stats.release()
    finally:
        first.release()
        second.release()

    assert list(config.jobs_dir().glob("*/manifest.json")) == []


def test_ui_cancellation_at_commit_boundary_still_launches_runner(
    mock_env_with_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    sql_path = _write_sql(data_root)
    first_lease = capacity.try_acquire_metadata("describe")
    second_lease = capacity.try_acquire_metadata("show tables")
    commit_started = threading.Event()
    allow_commit = threading.Event()
    runner_started = threading.Event()
    allow_runner = threading.Event()
    launched: list[Path] = []
    original_create = manifest.create_job

    def blocked_create(**kwargs):
        commit_started.set()
        assert allow_commit.wait(2)
        return original_create(**kwargs)

    async def fake_ttl() -> int:
        return 7200

    async def fake_launch_runner(job_dir: Path) -> int:
        runner_started.set()
        assert await asyncio.to_thread(allow_runner.wait, 2)
        launched.append(job_dir)
        return 0

    monkeypatch.setattr(manifest, "create_job", blocked_create)
    monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)
    monkeypatch.setattr("dispatch.process.launch_runner", fake_launch_runner)

    async def run() -> None:
        app = DispatchApp()
        prefill = {
            "source_type": "SqlFile",
            "dest_type": "Csv",
            "sql_file": str(sql_path),
            "table_name": "cancel_commit",
        }
        async with app.run_test(size=(140, 50)) as pilot:
            screen = NewJobScreen(data_root, prefill=prefill)
            app.push_screen(screen)
            await pilot.pause(0.5)
            monkeypatch.setattr(screen, "_confirm_launch", _confirm_launch)
            flow = asyncio.create_task(screen._launch_flow())
            await _wait_for_intent_count(data_root, 1)
            first_lease.release()
            assert await asyncio.to_thread(commit_started.wait, 2)
            flow.cancel()
            await asyncio.sleep(0)
            flow.cancel()
            allow_commit.set()
            assert await asyncio.to_thread(runner_started.wait, 2)
            flow.cancel()
            await asyncio.sleep(0)
            flow.cancel()
            allow_runner.set()
            await flow

    try:
        asyncio.run(run())
    finally:
        allow_commit.set()
        allow_runner.set()
        first_lease.release()
        second_lease.release()

    assert len(launched) == 1
    assert manifest.load(launched[0] / "manifest.json")["state"] == "Pending"


def test_concurrent_launches_atomically_create_only_one_last_pending_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
    jobs.create_job_if_slot_available(**_launch_args(tmp_path, "existing"), timeout=0)

    async def run() -> list[object]:
        return await asyncio.gather(
            jobs.create_job_when_capacity_available(
                **_launch_args(tmp_path, "contender_one"), timeout=1
            ),
            jobs.create_job_when_capacity_available(
                **_launch_args(tmp_path, "contender_two"), timeout=1
            ),
            return_exceptions=True,
        )

    results = asyncio.run(run())

    assert len([result for result in results if isinstance(result, tuple)]) == 1
    assert len([result for result in results if isinstance(result, capacity.CapacityBusy)]) == 1
    pending = [
        manifest.load(path)
        for path in config.jobs_dir().glob("*/manifest.json")
        if manifest.load(path)["state"] == "Pending"
    ]
    assert len(pending) == 2


@pytest.mark.parametrize(
    "failure",
    [
        capacity.CapacityBusy("two Dispatch jobs already occupy shared capacity"),
        capacity.CapacityTimeout("Dispatch launch capacity timed out after 30s"),
        capacity.CapacityLedgerError("cannot read capacity ledger"),
    ],
)
def test_new_job_surfaces_typed_capacity_failure(
    failure: RuntimeError,
    mock_env_with_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    sql_path = _write_sql(data_root)

    async def fake_ttl() -> int:
        return 7200

    async def fail_admission(**_kwargs: object) -> tuple[Path, manifest.JobManifest]:
        raise failure

    monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)
    monkeypatch.setattr(jobs, "create_job_when_capacity_available", fail_admission, raising=False)

    async def run() -> tuple[str, list[tuple[str, str | None]]]:
        app = DispatchApp()
        prefill = {
            "source_type": "SqlFile",
            "dest_type": "Csv",
            "sql_file": str(sql_path),
            "table_name": "capacity_failure",
        }
        async with app.run_test(size=(140, 50)) as pilot:
            screen = NewJobScreen(data_root, prefill=prefill)
            app.push_screen(screen)
            await pilot.pause(0.5)
            monkeypatch.setattr(screen, "_confirm_launch", _confirm_launch)
            notifications: list[tuple[str, str | None]] = []
            monkeypatch.setattr(
                screen,
                "notify",
                lambda message, severity=None, **_kwargs: notifications.append(
                    (str(message), severity)
                ),
            )

            await screen._launch_flow()
            await pilot.pause()
            return str(screen.query_one("#warning-text").render()), notifications

    warning, notifications = asyncio.run(run())

    assert str(failure) in warning
    assert (str(failure), "error") in notifications


async def _confirm_launch(*_args: object, **_kwargs: object) -> bool:
    return True

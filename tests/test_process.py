"""Tests for the TUI subprocess gateway."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from dispatch import capacity, impala, job_lifecycle, process

PROCESS_TIMEOUT = 2.0


def _pid_exists(pid: int) -> bool:
    return job_lifecycle.pid_is_alive(pid)


async def _wait_for(predicate) -> bool:
    deadline = time.monotonic() + PROCESS_TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _wait_for_pid(path: Path) -> int:
    """Wait until a child PID marker exists and its contents are readable."""
    deadline = time.monotonic() + PROCESS_TIMEOUT
    while time.monotonic() < deadline:
        try:
            return int(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            await asyncio.sleep(0.01)
    raise AssertionError(f"child PID marker was not ready: {path}")


def test_windows_resolver_prefers_exact_python_script_before_pathext_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    script = tmp_path / "kinit"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (tmp_path / "kinit.exe").write_bytes(b"legacy wrapper")
    monkeypatch.setenv("PATH", str(tmp_path))

    resolved = process.resolve_exec_argv(("kinit", "user@REALM"), windows=True)

    assert resolved == (sys.executable, str(script), "user@REALM")


@pytest.mark.asyncio
async def test_launch_runner_uses_detached_nohup_setsid_contract(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[tuple[str, ...], dict]] = []

    class FakeProc:
        pid = 4242

    async def fake_create_subprocess_exec(*argv: str, **kwargs):
        calls.append((argv, kwargs))
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    pid = await process.launch_runner(tmp_path / "job1")

    assert pid == 4242
    assert calls
    argv, kwargs = calls[0]
    assert argv[:5] == ("nohup", "setsid", process.sys.executable, "-m", "dispatch.runner")
    assert argv[-2:] == ("--job-dir", str(tmp_path / "job1"))
    assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] is asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] is asyncio.subprocess.DEVNULL


def test_cancel_process_group_sends_sigterm_to_group(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process.os,
        "killpg",
        lambda pid, sig: calls.append((pid, sig)),
        raising=False,
    )

    process.cancel_process_group(4242)

    assert calls == [(4242, process.signal.SIGTERM)]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM handler contract")
async def test_cancelled_impala_query_reaps_child_before_releasing_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = tmp_path / "started"
    terminated = tmp_path / "terminated"
    allow_exit = tmp_path / "allow-exit"
    shell = tmp_path / "impala-shell"
    shell.write_text(
        f"""#!{sys.executable}
import os
import signal
import sys
import time
from pathlib import Path

started = Path(os.environ["DISPATCH_TEST_CHILD_STARTED"])
terminated = Path(os.environ["DISPATCH_TEST_CHILD_TERMINATED"])
allow_exit = Path(os.environ["DISPATCH_TEST_CHILD_ALLOW_EXIT"])

def handle_term(_signum, _frame):
    terminated.write_text("terminated", encoding="utf-8")
    while not allow_exit.exists():
        time.sleep(0.01)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, handle_term)
started.write_text(str(os.getpid()), encoding="utf-8")
while True:
    signal.pause()
""",
        encoding="utf-8",
    )
    shell.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("DISPATCH_TEST_CHILD_STARTED", str(started))
    monkeypatch.setenv("DISPATCH_TEST_CHILD_TERMINATED", str(terminated))
    monkeypatch.setenv("DISPATCH_TEST_CHILD_ALLOW_EXIT", str(allow_exit))

    created: list[asyncio.subprocess.Process] = []
    original_create_subprocess_exec = asyncio.create_subprocess_exec

    async def capture_process(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        child = await original_create_subprocess_exec(*argv, **kwargs)
        created.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)

    existing = capacity.try_acquire_metadata("existing")
    task = asyncio.create_task(impala.query("SHOW TABLES IN aa_enc;"))
    pid = -1
    probe = None
    try:
        pid = await _wait_for_pid(started)
        task.cancel()
        assert await _wait_for(lambda: terminated.exists() or task.done())

        child_was_orphaned = task.done() and _pid_exists(pid)
        terminated_before_release = terminated.exists()
        try:
            probe = capacity.try_acquire_metadata("must-stay-blocked")
        except capacity.CapacityBusy:
            pass

        allow_exit.write_text("exit", encoding="utf-8")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_for(lambda: not _pid_exists(pid))

        assert child_was_orphaned is False
        assert terminated_before_release is True
        assert probe is None
    finally:
        allow_exit.write_text("exit", encoding="utf-8")
        for child in created:
            if child.returncode is None:
                child.kill()
            await child.wait()
        if probe is not None:
            probe.release()
        existing.release()


@pytest.mark.asyncio
async def test_run_exec_timeout_awaits_subprocess_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class FakeProcess:
        returncode = None

        async def communicate(self, *, input: bytes | None = None) -> tuple[bytes, bytes]:
            return b"", b""

    child = FakeProcess()

    async def fake_create_subprocess_exec(*_argv: str, **_kwargs: object) -> FakeProcess:
        return child

    async def timeout(awaitable, *, timeout: float | None = None):
        awaitable.close()
        raise asyncio.TimeoutError

    async def cleanup(proc: object) -> None:
        assert proc is child
        events.append("cleanup-started")
        await asyncio.sleep(0)
        events.append("cleanup-finished")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", timeout)
    monkeypatch.setattr(process, "_terminate_and_reap", cleanup)

    with pytest.raises(asyncio.TimeoutError):
        await process.run_exec("impala-shell", timeout=0.1)

    assert events == ["cleanup-started", "cleanup-finished"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows hard-termination contract")
async def test_windows_terminate_and_reap_stops_real_child(tmp_path: Path) -> None:
    started = tmp_path / "windows-child-started"
    script = (
        "import os,time\n"
        "from pathlib import Path\n"
        f"Path({str(started)!r}).write_text(str(os.getpid()),encoding='utf-8')\n"
        "while True: time.sleep(1)\n"
    )
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", script)

    try:
        pid = await _wait_for_pid(started)
        assert _pid_exists(pid)

        await process._terminate_and_reap(child)

        assert child.returncode is not None
        assert not _pid_exists(pid)
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()

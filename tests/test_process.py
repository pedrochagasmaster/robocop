"""Tests for the TUI subprocess gateway."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from dispatch import capacity, impala, process

PROCESS_TIMEOUT = 2.0


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for(predicate) -> bool:
    deadline = time.monotonic() + PROCESS_TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def test_windows_resolver_prefers_exact_python_script_before_pathext_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    script = tmp_path / "kinit"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (tmp_path / "kinit.exe").write_bytes(b"legacy wrapper")
    monkeypatch.setenv("PATH", str(tmp_path))

    resolved = process._resolve_exec_argv(("kinit", "user@REALM"), windows=True)

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

    existing = capacity.try_acquire_metadata("existing")
    task = asyncio.create_task(impala.query("SHOW TABLES IN aa_enc;"))
    pid = -1
    probe = None
    try:
        assert await _wait_for(started.exists)
        pid = int(started.read_text(encoding="utf-8"))
        task.cancel()
        assert await _wait_for(lambda: terminated.exists() or task.done())

        child_was_orphaned = task.done() and _pid_exists(pid)
        terminated_before_release = terminated.exists()
        try:
            probe = capacity.try_acquire_metadata("must-stay-blocked")
        except capacity.CapacityBusy:
            pass

        allow_exit.write_text("exit", encoding="utf-8")
        if not terminated_before_release and _pid_exists(pid):
            os.kill(pid, signal.SIGTERM)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_for(lambda: not _pid_exists(pid))

        assert child_was_orphaned is False
        assert terminated_before_release is True
        assert probe is None
    finally:
        allow_exit.write_text("exit", encoding="utf-8")
        if pid > 0 and _pid_exists(pid):
            os.kill(pid, signal.SIGKILL)
            await _wait_for(lambda: not _pid_exists(pid))
        if probe is not None:
            probe.release()
        existing.release()


@pytest.mark.asyncio
async def test_run_exec_timeout_still_terminates_and_reaps_child(tmp_path: Path) -> None:
    started = tmp_path / "timeout-started"
    terminated = tmp_path / "timeout-terminated"
    script = (
        "import os,signal,time\n"
        "from pathlib import Path\n"
        f"started=Path({str(started)!r})\n"
        f"terminated=Path({str(terminated)!r})\n"
        "def stop(_signum,_frame):\n"
        "    terminated.write_text('terminated',encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "started.write_text(str(os.getpid()),encoding='utf-8')\n"
        "while True: time.sleep(1)\n"
    )

    with pytest.raises(asyncio.TimeoutError):
        await process.run_exec(sys.executable, "-c", script, timeout=0.1)

    pid = int(started.read_text(encoding="utf-8"))
    assert terminated.exists()
    assert not _pid_exists(pid)

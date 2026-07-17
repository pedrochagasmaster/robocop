"""Single subprocess gateway for the Dispatch TUI."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Literal

from .asyncio_utils import await_uncancellable


def _resolve_exec_argv(argv: tuple[str, ...], *, windows: bool | None = None) -> tuple[str, ...]:
    """Resolve a command without letting Windows bypass PATH-injected Python mocks."""
    is_windows = os.name == "nt" if windows is None else windows
    if is_windows and not Path(argv[0]).parent.name:
        for directory in os.get_exec_path():
            candidate = Path(directory or os.curdir) / argv[0]
            if not candidate.is_file():
                continue
            try:
                with candidate.open("rb") as handle:
                    shebang = handle.readline().lower()
            except OSError:
                continue
            if shebang.startswith(b"#!") and b"python" in shebang:
                return (sys.executable, str(candidate), *argv[1:])

    return (shutil.which(argv[0]) or argv[0], *argv[1:])


async def _terminate_and_reap(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    await proc.wait()


async def run_exec(
    *argv: str, timeout: float | None = None, stdin_data: bytes | None = None
) -> tuple[int, str, str]:
    # Resolve bare names against PATH ourselves: Windows CreateProcess searches
    # System32 before PATH, so e.g. `klist` would hit the OS tool instead of a
    # PATH-injected mock. shutil.which honors PATH order on every platform; an
    # unresolvable name keeps its FileNotFoundError from create_subprocess_exec.
    resolved_argv = _resolve_exec_argv(argv)
    proc = await asyncio.create_subprocess_exec(
        *resolved_argv,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=stdin_data), timeout=timeout)
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(_terminate_and_reap(proc))
        await await_uncancellable(cleanup)
        raise
    except asyncio.TimeoutError:
        await _terminate_and_reap(proc)
        raise
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def launch_runner(job_dir: Path) -> int:
    proc = await asyncio.create_subprocess_exec(
        "nohup",
        "setsid",
        sys.executable,
        "-m",
        "dispatch.runner",
        "--job-dir",
        str(job_dir),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return proc.pid


CancelProcessResult = Literal["signaled", "missing"]


def cancel_process_group(pid: int) -> CancelProcessResult:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "missing"
    return "signaled"


def run_interactive(*argv: str) -> int:
    with subprocess.Popen(argv) as proc:
        return proc.wait()

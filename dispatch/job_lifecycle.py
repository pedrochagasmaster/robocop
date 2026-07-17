"""Shared process-liveness and stale Job transition rules."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import manifest

PENDING_ORPHAN_GRACE = timedelta(minutes=5)

_WINDOWS = os.name == "nt"
_WINDOWS_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True) if _WINDOWS else None
if _WINDOWS_KERNEL32 is not None:
    _WINDOWS_KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _WINDOWS_KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _WINDOWS_KERNEL32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    _WINDOWS_KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
    _WINDOWS_KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _WINDOWS_KERNEL32.CloseHandle.restype = wintypes.BOOL


@dataclass(frozen=True)
class Reconciliation:
    """A stale Job's Failed manifest and operator-facing reason."""

    manifest: manifest.JobManifest
    log_message: str


def pid_is_alive(pid: int) -> bool:
    """Return whether ``pid`` still names a live process."""
    if pid <= 0:
        return False
    if _WINDOWS:
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_is_alive(pid: int) -> bool:
    """Probe a Windows PID without sending the destructive ``os.kill`` signal."""
    if _WINDOWS_KERNEL32 is None:
        return True
    process_query_limited_information = 0x1000
    still_active = 259
    handle = _WINDOWS_KERNEL32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means a process exists; invalid parameter means it exited.
        return ctypes.get_last_error() != 87
    try:
        exit_code = wintypes.DWORD()
        if not _WINDOWS_KERNEL32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        _WINDOWS_KERNEL32.CloseHandle(handle)


def reconcile(
    item: manifest.JobManifest,
    modified_at: datetime | None,
    *,
    pid_probe: Callable[[int], bool] = pid_is_alive,
) -> Reconciliation | None:
    """Return the shared stale-Job transition, without performing file I/O."""
    current_time = datetime.now(timezone.utc)
    pid = item.get("pid")
    log_message: str | None = None

    if item["state"] == "Running" and pid is not None and not pid_probe(pid):
        log_message = f"[dispatch] stale runner pid {pid} not found; manifest marked Failed"
    elif item["state"] == "Pending" and pid is None:
        if modified_at is None:
            raise ValueError("Pending Job reconciliation requires its manifest modification time")
        if current_time - modified_at > PENDING_ORPHAN_GRACE:
            log_message = "[dispatch] Pending job exceeded startup grace; manifest marked Failed"

    if log_message is None:
        return None

    updated = item.copy()
    updated["state"] = "Failed"
    updated["exit_code"] = -1
    updated["finished_at"] = current_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    return Reconciliation(updated, log_message)

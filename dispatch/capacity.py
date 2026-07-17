"""Cross-process admission for Dispatch-managed Impala capacity.

Callers acquire metadata leases or submit a Pending-job callback. This module
owns the durable representation, locking, stale-owner recovery, and fairness
rules so none of those details leak into jobs or Impala call sites.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict, TypeVar

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

from . import config, manifest

T = TypeVar("T")

__all__ = [
    "CapacityBusy",
    "CapacityLedgerError",
    "CapacityTimeout",
    "MetadataLease",
    "admit_launch",
    "try_acquire_metadata",
]

CAPACITY_LIMIT = 2
LEDGER_VERSION = 1
# This is the one lifecycle value that cannot be imported from ``jobs``:
# Task 3 makes jobs depend on this module. Manifest validation, timestamps, and
# atomic state writes remain delegated to ``manifest`` instead of being copied.
_PENDING_ORPHAN_GRACE = timedelta(minutes=5)
_POLL_SECONDS = 0.05
_WINDOWS = os.name == "nt"
_WINDOWS_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True) if _WINDOWS else None
if _WINDOWS_KERNEL32 is not None:
    _WINDOWS_KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _WINDOWS_KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _WINDOWS_KERNEL32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    _WINDOWS_KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
    _WINDOWS_KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _WINDOWS_KERNEL32.CloseHandle.restype = wintypes.BOOL


class CapacityBusy(RuntimeError):
    """Raised when no shared capacity is available without waiting."""


class CapacityTimeout(RuntimeError):
    """Raised when a queued launch cannot be admitted before its deadline."""


class CapacityLedgerError(RuntimeError):
    """Raised when shared capacity state cannot be trusted or updated."""


class _MetadataOwner(TypedDict):
    token: str
    pid: int
    operation: str
    created_at: str


class _LaunchIntent(TypedDict):
    pid: int
    sequence: int
    created_at: str


class _JobReservation(TypedDict):
    job_id: str
    manifest_path: str


class _Ledger(TypedDict):
    version: int
    next_sequence: int
    metadata_owners: list[_MetadataOwner]
    launch_intents: list[_LaunchIntent]
    job_reservations: list[_JobReservation]


def _new_ledger() -> _Ledger:
    return {
        "version": LEDGER_VERSION,
        "next_sequence": 1,
        "metadata_owners": [],
        "launch_intents": [],
        "job_reservations": [],
    }


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _capacity_home(root: Path | None) -> Path:
    path = config.dispatch_home() if root is None else root / ".dispatch"
    return Path(os.path.abspath(path))


def _path_metadata(path: Path, label: str, *, missing_ok: bool = False) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise CapacityLedgerError(f"{label} does not exist: {path}") from None
    except OSError as exc:
        raise CapacityLedgerError(f"cannot inspect {label} {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise CapacityLedgerError(f"{label} must not be a symlink: {path}")
    return metadata


def _require_directory(path: Path, label: str, *, missing_ok: bool = False) -> bool:
    metadata = _path_metadata(path, label, missing_ok=missing_ok)
    if metadata is None:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        raise CapacityLedgerError(f"{label} is not a directory: {path}")
    return True


def _require_regular_file(path: Path, label: str, *, missing_ok: bool = False) -> bool:
    metadata = _path_metadata(path, label, missing_ok=missing_ok)
    if metadata is None:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        raise CapacityLedgerError(f"{label} is not a regular file: {path}")
    return True


def _ensure_private_home(home: Path) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise CapacityLedgerError(f"cannot create capacity directory {home}: {exc}") from exc
    _require_directory(home, "capacity directory")
    try:
        home.chmod(0o700)
    except OSError as exc:
        raise CapacityLedgerError(f"cannot protect capacity directory {home}: {exc}") from exc


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_keys(item: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != keys:
        raise CapacityLedgerError(f"invalid {label} in capacity ledger")
    return item


def _validate_ledger(data: Any) -> _Ledger:
    ledger = _require_keys(
        data,
        {
            "version",
            "next_sequence",
            "metadata_owners",
            "launch_intents",
            "job_reservations",
        },
        "root",
    )
    if ledger["version"] != LEDGER_VERSION:
        raise CapacityLedgerError("unsupported capacity ledger version")
    if not _is_int(ledger["next_sequence"]) or ledger["next_sequence"] < 1:
        raise CapacityLedgerError("invalid next sequence in capacity ledger")

    owners = ledger["metadata_owners"]
    intents = ledger["launch_intents"]
    reservations = ledger["job_reservations"]
    if (
        not isinstance(owners, list)
        or not isinstance(intents, list)
        or not isinstance(reservations, list)
    ):
        raise CapacityLedgerError("capacity ledger collections must be lists")

    seen_tokens: set[str] = set()
    for raw_owner in owners:
        owner = _require_keys(
            raw_owner, {"token", "pid", "operation", "created_at"}, "metadata owner"
        )
        if (
            not isinstance(owner["token"], str)
            or not owner["token"]
            or owner["token"] in seen_tokens
            or not _is_int(owner["pid"])
            or owner["pid"] <= 0
            or not isinstance(owner["operation"], str)
            or not owner["operation"]
            or not isinstance(owner["created_at"], str)
            or not owner["created_at"]
        ):
            raise CapacityLedgerError("invalid metadata owner in capacity ledger")
        seen_tokens.add(owner["token"])

    seen_sequences: set[int] = set()
    for raw_intent in intents:
        intent = _require_keys(raw_intent, {"pid", "sequence", "created_at"}, "launch intent")
        if (
            not _is_int(intent["pid"])
            or intent["pid"] <= 0
            or not _is_int(intent["sequence"])
            or intent["sequence"] < 1
            or intent["sequence"] in seen_sequences
            or not isinstance(intent["created_at"], str)
            or not intent["created_at"]
        ):
            raise CapacityLedgerError("invalid launch intent in capacity ledger")
        seen_sequences.add(intent["sequence"])

    seen_jobs: set[str] = set()
    for raw_reservation in reservations:
        reservation = _require_keys(raw_reservation, {"job_id", "manifest_path"}, "job reservation")
        if (
            not isinstance(reservation["job_id"], str)
            or not reservation["job_id"]
            or reservation["job_id"] in seen_jobs
            or not isinstance(reservation["manifest_path"], str)
            or not reservation["manifest_path"]
        ):
            raise CapacityLedgerError("invalid job reservation in capacity ledger")
        seen_jobs.add(reservation["job_id"])
    if seen_sequences and ledger["next_sequence"] <= max(seen_sequences):
        raise CapacityLedgerError("next sequence does not follow queued launch intents")

    return data


def _load_ledger(path: Path) -> _Ledger:
    if not _require_regular_file(path, "capacity ledger", missing_ok=True):
        return _new_ledger()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CapacityLedgerError(f"capacity ledger is not a regular file: {path}")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                return _validate_ledger(json.load(handle))
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except FileNotFoundError:
        return _new_ledger()
    except CapacityLedgerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise CapacityLedgerError(f"cannot read capacity ledger: {exc}") from exc


def _replace_with_retry(source: Path, destination: Path) -> None:
    delays = (0.02, 0.05, 0.1)
    for delay in delays:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(source, destination)


def _fsync_directory(directory: Path) -> None:
    """Durably record a same-directory replacement where directory fsync exists."""
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not directory_flag:
        return
    flags = os.O_RDONLY | directory_flag | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                raise
    finally:
        os.close(descriptor)


def _save_ledger(path: Path, ledger: _Ledger) -> None:
    _validate_ledger(ledger)
    _require_regular_file(path, "capacity ledger", missing_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(ledger, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temporary, path)
            path.chmod(0o600)
            _fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except (OSError, TypeError, ValueError) as exc:
        raise CapacityLedgerError(f"cannot update capacity ledger: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _lock_file(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _locked_home(root: Path | None) -> Iterator[Path]:
    home = _capacity_home(root)
    lock_path = home / "capacity.lock"
    try:
        _ensure_private_home(home)
        _require_regular_file(lock_path, "capacity lock", missing_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"capacity lock is not a regular file: {lock_path}")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "r+b")
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError as exc:
        raise CapacityLedgerError(f"cannot open capacity lock: {exc}") from exc

    locked = False
    try:
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            _lock_file(handle)
            locked = True
        except OSError as exc:
            raise CapacityLedgerError(f"cannot lock capacity ledger: {exc}") from exc
        yield home
    finally:
        try:
            if locked:
                _unlock_file(handle)
        except OSError as exc:
            raise CapacityLedgerError(f"cannot unlock capacity ledger: {exc}") from exc
        finally:
            handle.close()


def _pid_is_alive(pid: int) -> bool:
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
        # Access denied is evidence that a process exists. Invalid parameter is
        # Windows' normal response for a PID that no longer exists.
        return ctypes.get_last_error() != 87
    try:
        exit_code = wintypes.DWORD()
        if not _WINDOWS_KERNEL32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        _WINDOWS_KERNEL32.CloseHandle(handle)


def _fail_stale_manifest(path: Path, item: manifest.JobManifest) -> None:
    updated = item.copy()
    updated["state"] = "Failed"
    updated["exit_code"] = -1
    updated["finished_at"] = manifest.now_utc()
    try:
        _require_directory(path.parent, "job directory")
        _require_regular_file(path, "job manifest")
        manifest.write(path, updated)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CapacityLedgerError(f"cannot reconcile job manifest {path}: {exc}") from exc


def _load_job_manifest(path: Path) -> manifest.JobManifest | None:
    if not _require_regular_file(path, "job manifest", missing_ok=True):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CapacityLedgerError(f"job manifest is not a regular file: {path}")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                data = json.load(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        manifest.validate(data)
        return data
    except FileNotFoundError:
        return None
    except CapacityLedgerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        raise CapacityLedgerError(f"cannot read job manifest {path}: {exc}") from exc


def _active_reservation(path: Path) -> _JobReservation | None:
    try:
        item = _load_job_manifest(path)
        if item is None:
            return None
        state = item["state"]
        pid = item.get("pid")
        if state == "Running" and pid is not None and not _pid_is_alive(pid):
            _fail_stale_manifest(path, item)
            return None
        if state == "Pending" and pid is None:
            metadata = _path_metadata(path, "job manifest")
            assert metadata is not None
            modified = datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc)
            if datetime.now(timezone.utc) - modified > _PENDING_ORPHAN_GRACE:
                _fail_stale_manifest(path, item)
                return None
    except (FileNotFoundError, NotADirectoryError):
        return None
    except CapacityLedgerError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CapacityLedgerError(f"cannot read job manifest {path}: {exc}") from exc

    if state not in {"Pending", "Running"}:
        return None
    job_id = item["id"]
    if not isinstance(job_id, str) or not job_id:
        raise CapacityLedgerError(f"invalid job id in manifest {path}")
    return {"job_id": job_id, "manifest_path": str(path)}


def _validated_manifest_path(path: Path, jobs_root: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    if (
        candidate.name != "manifest.json"
        or candidate.parent.parent != jobs_root
        or candidate.parent.name.startswith(".")
    ):
        raise CapacityLedgerError(f"job reservation escapes the Dispatch jobs directory: {path}")
    if not _require_directory(candidate.parent, "job directory", missing_ok=True):
        return candidate
    _require_regular_file(candidate, "job manifest", missing_ok=True)
    return candidate


def _reservation_paths(ledger: _Ledger, jobs_root: Path) -> set[Path]:
    paths: set[Path] = set()
    for reservation in ledger["job_reservations"]:
        paths.add(_validated_manifest_path(Path(reservation["manifest_path"]), jobs_root))

    if not _require_directory(jobs_root, "jobs directory", missing_ok=True):
        return paths
    try:
        entries = list(jobs_root.iterdir())
    except OSError as exc:
        raise CapacityLedgerError(f"cannot scan Dispatch jobs: {exc}") from exc
    for entry in entries:
        metadata = _path_metadata(entry, "job directory entry", missing_ok=True)
        if metadata is None:
            continue
        if entry.name.startswith(".") and stat.S_ISREG(metadata.st_mode):
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise CapacityLedgerError(f"job directory entry is not a directory: {entry}")
        paths.add(_validated_manifest_path(entry / "manifest.json", jobs_root))
    return paths


def _reconcile(ledger: _Ledger, home: Path) -> None:
    ledger["metadata_owners"] = [
        owner for owner in ledger["metadata_owners"] if _pid_is_alive(owner["pid"])
    ]
    ledger["launch_intents"] = [
        intent for intent in ledger["launch_intents"] if _pid_is_alive(intent["pid"])
    ]

    jobs_root = home / "jobs"
    reservations: dict[str, _JobReservation] = {}
    for path in sorted(_reservation_paths(ledger, jobs_root)):
        reservation = _active_reservation(path)
        if reservation is None:
            continue
        if reservation["job_id"] in reservations:
            raise CapacityLedgerError(
                f"duplicate active job id in capacity ledger: {reservation['job_id']}"
            )
        reservations[reservation["job_id"]] = reservation
    ledger["job_reservations"] = list(reservations.values())


def _load_reconciled(home: Path) -> tuple[Path, _Ledger]:
    path = home / "capacity.json"
    ledger = _load_ledger(path)
    _reconcile(ledger, home)
    return path, ledger


def _occupied(ledger: _Ledger) -> int:
    return len(ledger["metadata_owners"]) + len(ledger["job_reservations"])


def _stats_operation(operation: str) -> bool:
    return "stat" in operation.casefold()


class MetadataLease:
    """One shared metadata slot, released by opaque token."""

    def __init__(self, token: str, root: Path | None) -> None:
        self._token = token
        self._root = root
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        """Idempotently release only this lease's ledger entry."""
        with self._release_lock:
            if self._released:
                return
            with _locked_home(self._root) as home:
                path, ledger = _load_reconciled(home)
                ledger["metadata_owners"] = [
                    owner for owner in ledger["metadata_owners"] if owner["token"] != self._token
                ]
                _save_ledger(path, ledger)
            self._released = True


def try_acquire_metadata(operation: str, root: Path | None = None) -> MetadataLease:
    """Acquire a metadata slot immediately or raise :class:`CapacityBusy`."""
    if not operation:
        raise ValueError("operation must not be empty")
    token = uuid.uuid4().hex
    with _locked_home(root) as home:
        path, ledger = _load_reconciled(home)
        launch_has_priority = _stats_operation(operation) and bool(ledger["launch_intents"])
        if launch_has_priority or _occupied(ledger) >= CAPACITY_LIMIT:
            _save_ledger(path, ledger)
            raise CapacityBusy("Dispatch Impala capacity is busy")
        ledger["metadata_owners"].append(
            {
                "token": token,
                "pid": os.getpid(),
                "operation": operation,
                "created_at": _now_utc(),
            }
        )
        _save_ledger(path, ledger)
    return MetadataLease(token, root)


def _remove_intent(ledger: _Ledger, sequence: int) -> None:
    ledger["launch_intents"] = [
        intent for intent in ledger["launch_intents"] if intent["sequence"] != sequence
    ]


def _discard_launch_intent(root: Path | None, sequence: int) -> None:
    with _locked_home(root) as home:
        path, ledger = _load_reconciled(home)
        _remove_intent(ledger, sequence)
        _save_ledger(path, ledger)


def _wait_for_retry() -> None:
    time.sleep(_POLL_SECONDS)


def admit_launch(
    create_pending: Callable[[], T],
    timeout: float = 30,
    root: Path | None = None,
) -> T:
    """Admit one Pending-job callback fairly within the shared two-slot cap.

    Two active jobs fail immediately. Metadata occupancy queues the launch in
    FIFO order for at most ``timeout`` seconds. The callback runs while the
    ledger lock is held, making admission and Pending-manifest creation atomic
    to every other Dispatch process.
    """
    if timeout < 0:
        raise ValueError("timeout must not be negative")
    started = time.monotonic()
    sequence: int | None = None
    try:
        with _locked_home(root) as home:
            path, ledger = _load_reconciled(home)
            if len(ledger["job_reservations"]) >= CAPACITY_LIMIT:
                _save_ledger(path, ledger)
                raise CapacityBusy("two Dispatch jobs already occupy shared capacity")
            sequence = ledger["next_sequence"]
            ledger["next_sequence"] += 1
            ledger["launch_intents"].append(
                {"pid": os.getpid(), "sequence": sequence, "created_at": _now_utc()}
            )
            _save_ledger(path, ledger)

        assert sequence is not None
        while True:
            with _locked_home(root) as home:
                path, ledger = _load_reconciled(home)
                if len(ledger["job_reservations"]) >= CAPACITY_LIMIT:
                    raise CapacityBusy("two Dispatch jobs already occupy shared capacity")

                sequences = [intent["sequence"] for intent in ledger["launch_intents"]]
                is_first = bool(sequences) and sequence == min(sequences)
                if is_first and _occupied(ledger) < CAPACITY_LIMIT:
                    result = create_pending()
                    _remove_intent(ledger, sequence)
                    _reconcile(ledger, home)
                    _save_ledger(path, ledger)
                    sequence = None
                    return result

                if time.monotonic() - started >= timeout:
                    raise CapacityTimeout(f"Dispatch launch capacity timed out after {timeout:g}s")

                _save_ledger(path, ledger)
            _wait_for_retry()
    finally:
        if sequence is not None:
            _discard_launch_intent(root, sequence)

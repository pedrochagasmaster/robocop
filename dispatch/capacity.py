"""Cross-process admission for Dispatch-managed Impala capacity.

Callers acquire metadata leases or submit a Pending-job callback. This module
owns the durable representation, locking, stale-owner recovery, and fairness
rules so none of those details leak into jobs or Impala call sites.
"""

from __future__ import annotations

import asyncio
import errno
import json
import math
import os
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict, TypeVar, cast

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

from . import config, job_lifecycle, manifest
from .asyncio_utils import await_uncancellable

T = TypeVar("T")

__all__ = [
    "CapacityBusy",
    "CapacityLedgerError",
    "CapacityTimeout",
    "MetadataLease",
    "admit_launch",
    "admit_launch_async",
    "try_acquire_metadata",
]

CAPACITY_LIMIT = 2
LEDGER_VERSION = 2
_POLL_SECONDS = 0.05
_ASYNC_POLL_SECONDS = 0.25
_LOCK_POLL_SECONDS = 0.01
_METADATA_LOCK_TIMEOUT_SECONDS = 0.25
_MIGRATED_INTENT_TTL_SECONDS = 30.0


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
    deadline_at: float


class _JobReservation(TypedDict):
    job_id: str
    manifest_path: str


class _Ledger(TypedDict):
    version: int
    next_sequence: int
    metadata_owners: list[_MetadataOwner]
    launch_intents: list[_LaunchIntent]
    job_reservations: list[_JobReservation]


class _LockDeadline(TimeoutError):
    """Raised internally when the caller's lock budget expires."""


class _LaunchCancelled(RuntimeError):
    """Raised internally when cancellation wins before commit."""


class _LaunchControl:
    """Synchronize async cancellation with the callback commit boundary."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._committing = False

    def cancel_before_commit(self) -> bool:
        with self._lock:
            if self._committing:
                return False
            self._cancelled = True
            return True

    def begin_commit(self, deadline: float | None = None) -> bool:
        with self._lock:
            if self._cancelled:
                return False
            if deadline is not None and time.monotonic() >= deadline:
                raise CapacityTimeout("Dispatch launch capacity timed out before commit")
            self._committing = True
            return True


def _new_ledger() -> _Ledger:
    return {
        "version": LEDGER_VERSION,
        "next_sequence": 1,
        "metadata_owners": [],
        "launch_intents": [],
        "job_reservations": [],
    }


def _now_utc() -> str:
    return manifest.now_utc()


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


def _validate_metadata_owners(owners: list[Any]) -> None:
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


def _validate_launch_intents(intents: list[Any], intent_keys: set[str]) -> set[int]:
    seen_sequences: set[int] = set()
    for raw_intent in intents:
        intent = _require_keys(raw_intent, intent_keys, "launch intent")
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
        if "deadline_at" in intent and (
            not isinstance(intent["deadline_at"], (int, float))
            or isinstance(intent["deadline_at"], bool)
            or not math.isfinite(intent["deadline_at"])
        ):
            raise CapacityLedgerError("invalid launch intent in capacity ledger")
        seen_sequences.add(intent["sequence"])
    return seen_sequences


def _validate_job_reservations(reservations: list[Any]) -> None:
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


def _validate_ledger_version(
    data: Any,
    *,
    expected_version: int,
    intent_keys: set[str],
) -> dict[str, Any]:
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
    if ledger["version"] != expected_version:
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

    _validate_metadata_owners(owners)
    seen_sequences = _validate_launch_intents(intents, intent_keys)
    _validate_job_reservations(reservations)
    if seen_sequences and ledger["next_sequence"] <= max(seen_sequences):
        raise CapacityLedgerError("next sequence does not follow queued launch intents")

    return ledger


def _normalize_ledger(data: Any) -> Any:
    if not isinstance(data, dict) or data.get("version") != 1:
        return data
    legacy = _validate_ledger_version(
        data,
        expected_version=1,
        intent_keys={"pid", "sequence", "created_at"},
    )
    deadline_at = time.time() + _MIGRATED_INTENT_TTL_SECONDS
    return {
        "version": LEDGER_VERSION,
        "next_sequence": legacy["next_sequence"],
        "metadata_owners": [dict(owner) for owner in legacy["metadata_owners"]],
        "launch_intents": [
            {**intent, "deadline_at": deadline_at} for intent in legacy["launch_intents"]
        ],
        "job_reservations": [dict(reservation) for reservation in legacy["job_reservations"]],
    }


def _validate_ledger(data: Any) -> _Ledger:
    normalized = _normalize_ledger(data)
    validated = _validate_ledger_version(
        normalized,
        expected_version=LEDGER_VERSION,
        intent_keys={"pid", "sequence", "created_at", "deadline_at"},
    )
    return cast(_Ledger, validated)


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


def _lock_posix(handle: Any, deadline: float | None) -> None:
    if deadline is None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise _LockDeadline from None
            time.sleep(min(_LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _lock_windows(handle: Any, deadline: float | None) -> None:
    if deadline is None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if time.monotonic() >= deadline:
                raise _LockDeadline from None
            time.sleep(min(_LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _lock_file(handle: Any, deadline: float | None = None) -> None:
    if fcntl is not None:
        _lock_posix(handle, deadline)
    else:
        _lock_windows(handle, deadline)


def _unlock_file(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _locked_home(root: Path | None, deadline: float | None = None) -> Iterator[Path]:
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
            _lock_file(handle, deadline)
            locked = True
        except _LockDeadline:
            raise
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


def _same_manifest_snapshot(current: os.stat_result, loaded: os.stat_result) -> bool:
    return (
        current.st_dev,
        current.st_ino,
        current.st_mtime_ns,
    ) == (
        loaded.st_dev,
        loaded.st_ino,
        loaded.st_mtime_ns,
    )


def _write_reconciled_manifest(
    path: Path,
    item: manifest.JobManifest,
    loaded_metadata: os.stat_result,
) -> bool:
    try:
        _require_directory(path.parent, "job directory")
        current_metadata = _path_metadata(path, "job manifest", missing_ok=True)
        if current_metadata is None:
            return False
        if not stat.S_ISREG(current_metadata.st_mode):
            raise CapacityLedgerError(f"job manifest is not a regular file: {path}")
        if not _same_manifest_snapshot(current_metadata, loaded_metadata):
            return False
        return manifest.write_if_unchanged(path, item, loaded_metadata)
    except CapacityLedgerError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CapacityLedgerError(f"cannot reconcile job manifest {path}: {exc}") from exc


def _load_job_manifest(path: Path) -> tuple[manifest.JobManifest, os.stat_result] | None:
    if not _require_regular_file(path, "job manifest", missing_ok=True):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise CapacityLedgerError(f"job manifest is not a regular file: {path}")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                data = json.load(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        manifest.validate(data)
        return data, metadata
    except FileNotFoundError:
        return None
    except CapacityLedgerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        raise CapacityLedgerError(f"cannot read job manifest {path}: {exc}") from exc


def _active_reservation(path: Path) -> _JobReservation | None:
    try:
        while True:
            loaded = _load_job_manifest(path)
            if loaded is None:
                return None
            item, loaded_metadata = loaded
            state = item["state"]
            modified_at = datetime.fromtimestamp(loaded_metadata.st_mtime, tz=timezone.utc)
            reconciliation = job_lifecycle.reconcile(item, modified_at)
            if reconciliation is None:
                break
            if _write_reconciled_manifest(path, reconciliation.manifest, loaded_metadata):
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
        owner for owner in ledger["metadata_owners"] if job_lifecycle.pid_is_alive(owner["pid"])
    ]
    now = time.time()
    ledger["launch_intents"] = [
        intent
        for intent in ledger["launch_intents"]
        if job_lifecycle.pid_is_alive(intent["pid"]) and intent["deadline_at"] > now
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
    try:
        with _locked_home(root, time.monotonic() + _METADATA_LOCK_TIMEOUT_SECONDS) as home:
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
    except _LockDeadline:
        raise CapacityBusy("Dispatch capacity ledger is busy") from None
    return MetadataLease(token, root)


def _remove_intent(ledger: _Ledger, sequence: int) -> None:
    ledger["launch_intents"] = [
        intent for intent in ledger["launch_intents"] if intent["sequence"] != sequence
    ]


def _discard_launch_intent(
    root: Path | None,
    sequence: int,
    deadline: float | None = None,
) -> bool:
    try:
        with _locked_home(root, deadline) as home:
            path, ledger = _load_reconciled(home)
            _remove_intent(ledger, sequence)
            _save_ledger(path, ledger)
    except _LockDeadline:
        return False
    return True


def _launch_timeout(timeout: float) -> CapacityTimeout:
    return CapacityTimeout(f"Dispatch launch capacity timed out after {timeout:g}s")


def _register_launch_intent(
    root: Path | None,
    deadline: float,
    deadline_at: float,
    timeout: float,
) -> int:
    try:
        with _locked_home(root, deadline) as home:
            path, ledger = _load_reconciled(home)
            _ensure_launch_slot_available(path, ledger)
            if time.monotonic() >= deadline:
                _save_ledger(path, ledger)
                raise _launch_timeout(timeout)
            sequence = ledger["next_sequence"]
            ledger["next_sequence"] += 1
            ledger["launch_intents"].append(
                {
                    "pid": os.getpid(),
                    "sequence": sequence,
                    "created_at": _now_utc(),
                    "deadline_at": deadline_at,
                }
            )
            _save_ledger(path, ledger)
            return sequence
    except _LockDeadline:
        raise _launch_timeout(timeout) from None


def _ensure_launch_slot_available(path: Path, ledger: _Ledger) -> None:
    if len(ledger["job_reservations"]) < CAPACITY_LIMIT:
        return
    _save_ledger(path, ledger)
    raise CapacityBusy("two Dispatch jobs already occupy shared capacity")


def _commit_pending(
    create_pending: Callable[[], T],
    control: _LaunchControl,
    ledger: _Ledger,
    home: Path,
    path: Path,
    *,
    sequence: int | None = None,
    deadline: float | None = None,
) -> T:
    if not control.begin_commit(deadline):
        raise _LaunchCancelled
    result = create_pending()
    if sequence is not None:
        _remove_intent(ledger, sequence)
    _reconcile(ledger, home)
    _save_ledger(path, ledger)
    return result


def _try_admit_registered_launch(
    create_pending: Callable[[], T],
    sequence: int,
    root: Path | None,
    deadline: float,
    timeout: float,
    control: _LaunchControl,
) -> tuple[bool, T | None]:
    try:
        with _locked_home(root, deadline) as home:
            path, ledger = _load_reconciled(home)
            _ensure_launch_slot_available(path, ledger)
            if time.monotonic() >= deadline:
                _save_ledger(path, ledger)
                raise _launch_timeout(timeout)

            sequences = [intent["sequence"] for intent in ledger["launch_intents"]]
            if sequence not in sequences:
                _save_ledger(path, ledger)
                raise _launch_timeout(timeout)
            is_first = sequence == min(sequences)
            if not is_first or _occupied(ledger) >= CAPACITY_LIMIT:
                _save_ledger(path, ledger)
                return False, None
            result = _commit_pending(
                create_pending,
                control,
                ledger,
                home,
                path,
                sequence=sequence,
                deadline=deadline,
            )
            return True, result
    except _LockDeadline:
        raise _launch_timeout(timeout) from None


def _try_admit_immediately(
    create_pending: Callable[[], T],
    root: Path | None,
    control: _LaunchControl,
) -> T:
    try:
        with _locked_home(root, time.monotonic()) as home:
            path, ledger = _load_reconciled(home)
            _ensure_launch_slot_available(path, ledger)
            if ledger["launch_intents"] or _occupied(ledger) >= CAPACITY_LIMIT:
                _save_ledger(path, ledger)
                raise _launch_timeout(0)
            return _commit_pending(create_pending, control, ledger, home, path)
    except _LockDeadline:
        raise _launch_timeout(0) from None


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
    if timeout == 0:
        return _try_admit_immediately(create_pending, root, _LaunchControl())
    deadline = time.monotonic() + timeout
    deadline_at = time.time() + timeout
    sequence: int | None = None
    control = _LaunchControl()
    try:
        sequence = _register_launch_intent(root, deadline, deadline_at, timeout)
        while True:
            admitted, result = _try_admit_registered_launch(
                create_pending,
                sequence,
                root,
                deadline,
                timeout,
                control,
            )
            if admitted:
                sequence = None
                return cast(T, result)
            time.sleep(_POLL_SECONDS)
    finally:
        if sequence is not None:
            _discard_launch_intent(root, sequence, time.monotonic())


async def _discard_launch_intent_async(
    root: Path | None,
    sequence: int,
    deadline: float | None,
) -> None:
    cleanup = asyncio.create_task(
        asyncio.to_thread(_discard_launch_intent, root, sequence, deadline)
    )
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await await_uncancellable(cleanup)
        raise


async def _await_admission_attempt(
    attempt: asyncio.Task[T],
    control: _LaunchControl,
) -> T:
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError as cancelled:
        if control.cancel_before_commit():
            try:
                await await_uncancellable(attempt)
            except (_LaunchCancelled, CapacityBusy, CapacityTimeout):
                pass
            raise cancelled
        return await await_uncancellable(attempt)


async def _admit_immediately_async(
    create_pending: Callable[[], T],
    root: Path | None,
) -> T:
    control = _LaunchControl()
    attempt: asyncio.Task[T] = asyncio.create_task(
        asyncio.to_thread(_try_admit_immediately, create_pending, root, control)
    )
    return await _await_admission_attempt(attempt, control)


async def _try_admit_registered_launch_async(
    create_pending: Callable[[], T],
    sequence: int,
    root: Path | None,
    deadline: float,
    timeout: float,
    control: _LaunchControl,
) -> tuple[bool, T | None]:
    attempt: asyncio.Task[tuple[bool, T | None]] = asyncio.create_task(
        asyncio.to_thread(
            _try_admit_registered_launch,
            create_pending,
            sequence,
            root,
            deadline,
            timeout,
            control,
        )
    )
    return await _await_admission_attempt(attempt, control)


async def admit_launch_async(
    create_pending: Callable[[], T],
    timeout: float = 30,
    root: Path | None = None,
) -> T:
    """Admit one Pending callback with FIFO async waits and safe cancellation.

    One durable intent survives every 250ms async wait. Cancellation before the
    commit boundary removes that intent and raises ``CancelledError``. Once the
    callback has crossed the commit boundary, cancellation is suppressed so the
    caller receives the Pending result and can finish runner handoff.
    """
    if timeout < 0:
        raise ValueError("timeout must not be negative")
    if timeout == 0:
        return await _admit_immediately_async(create_pending, root)
    deadline = time.monotonic() + timeout
    deadline_at = time.time() + timeout
    control = _LaunchControl()
    sequence: int | None = None
    cleanup_deadline: float | None = deadline

    registration = asyncio.create_task(
        asyncio.to_thread(
            _register_launch_intent,
            root,
            deadline,
            deadline_at,
            timeout,
        )
    )
    try:
        try:
            sequence = await asyncio.shield(registration)
        except asyncio.CancelledError as cancelled:
            cleanup_deadline = None
            try:
                sequence = await await_uncancellable(registration)
            except Exception:
                raise cancelled
            raise cancelled

        while True:
            try:
                admitted, result = await _try_admit_registered_launch_async(
                    create_pending,
                    sequence,
                    root,
                    deadline,
                    timeout,
                    control,
                )
            except asyncio.CancelledError:
                cleanup_deadline = None
                raise
            if admitted:
                sequence = None
                return cast(T, result)
            await asyncio.sleep(min(_ASYNC_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    finally:
        if sequence is not None:
            await _discard_launch_intent_async(root, sequence, cleanup_deadline)

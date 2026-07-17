"""Offline usage telemetry: who uses Dispatch and how.

Append-only JSONL events to a private per-user log and, when writable, a
shared Edge Node rollup under ``/ads_storage/dispatch/telemetry``. Never raises
into product paths. Opt out with ``DISPATCH_TELEMETRY=0``.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import stat
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, TextIO

from . import config
from .version import __version__

# Edge Nodes are POSIX, so flock is the real locking path. Windows only runs
# this module during operator test runs (release verify); there msvcrt takes
# over, matching the cross-platform file-lock approach in dispatch.capacity.
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

logger = logging.getLogger("dispatch.telemetry")

_FALSEY = {"0", "false", "off", "no"}
_session_id: str | None = None
_session_started_at: datetime | None = None

DEFAULT_SHARED_DIR = Path("/ads_storage/dispatch/telemetry")
ScreenName = Literal["overview", "new_job", "history", "browser", "help", "job_detail"]
RefusalReason = Literal["slot_cap", "kerberos", "validation"]
_SCREEN_NAMES = frozenset({"overview", "new_job", "history", "browser", "help", "job_detail"})
_REFUSAL_REASONS = frozenset({"slot_cap", "kerberos", "validation"})
_QUEUE_CAPACITY = 256
_STOP_WRITER = object()


@dataclass(frozen=True)
class _WriteRequest:
    private_path: Path
    shared_path: Path
    line: str


@dataclass(frozen=True)
class _FlushRequest:
    done: threading.Event


_write_queue: queue.Queue[object] = queue.Queue(maxsize=_QUEUE_CAPACITY)
_writer_thread: threading.Thread | None = None
_writer_guard = threading.Lock()


def reset_session_for_tests() -> None:
    """Clear process-local session state (tests only)."""
    global _session_id, _session_started_at
    flush(timeout=1)
    _stop_writer_for_tests()
    _session_id = None
    _session_started_at = None


def enabled() -> bool:
    raw = os.environ.get("DISPATCH_TELEMETRY", "1").strip().lower()
    return raw not in _FALSEY


def session_id() -> str:
    global _session_id
    if _session_id is None:
        _session_id = uuid.uuid4().hex
    return _session_id


def private_telemetry_dir(user: str | None = None) -> Path:
    return config.dispatch_home(user) / "telemetry"


def private_events_path(user: str | None = None) -> Path:
    return private_telemetry_dir(user) / "events.jsonl"


def shared_telemetry_dir() -> Path:
    override = os.environ.get("DISPATCH_TELEMETRY_DIR")
    if override:
        return Path(override)
    return DEFAULT_SHARED_DIR


def shared_user_events_path(user: str | None = None) -> Path:
    name = user or config.current_user()
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("telemetry username must be one path component")
    return shared_telemetry_dir() / "users" / f"{name}.jsonl"


def _enqueue(event: str, props: dict[str, Any]) -> None:
    """Queue one validated event without performing filesystem I/O."""
    if not enabled():
        return
    try:
        user = config.current_user()
        record = {
            "ts": _now_utc(),
            "event": event,
            "user": user,
            "session_id": session_id(),
            "version": __version__,
            "props": props,
        }
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        request = _WriteRequest(
            private_path=private_events_path(user),
            shared_path=shared_user_events_path(user),
            line=line,
        )
        _ensure_writer()
        _write_queue.put_nowait(request)
    except queue.Full:
        logger.debug("telemetry queue full; dropping %s", event)
    except Exception:
        logger.debug("telemetry emit failed for %s", event, exc_info=True)


def note_session_start(*, cwd: Path | None = None) -> None:
    global _session_started_at
    _session_started_at = datetime.now(timezone.utc)
    props: dict[str, Any] = {}
    if cwd is not None:
        props["cwd_basename"] = cwd.name or "<root>"
    _enqueue("session_start", props)


def note_session_end() -> None:
    props: dict[str, Any] = {}
    if _session_started_at is not None:
        props["duration_s"] = int(
            (datetime.now(timezone.utc) - _session_started_at).total_seconds()
        )
    _enqueue("session_end", props)


def note_screen_view(screen: ScreenName) -> None:
    if screen not in _SCREEN_NAMES:
        logger.debug("dropping unknown telemetry screen: %s", screen)
        return
    _enqueue("screen_view", {"screen": screen})


def note_job_launched(*, job_id: str, source: str, destination: str) -> None:
    _enqueue(
        "job_launched",
        {
            "job_id": job_id,
            "source": source,
            "destination": destination,
        },
    )


def note_launch_refused(reason: RefusalReason) -> None:
    if reason not in _REFUSAL_REASONS:
        logger.debug("dropping unknown telemetry refusal reason: %s", reason)
        return
    _enqueue("launch_refused", {"reason": reason})


def note_job_cancelled(job_id: str) -> None:
    _enqueue("job_cancelled", {"job_id": job_id})


def flush(*, timeout: float = 1.0) -> bool:
    """Wait for events queued before this call; intended for tests and shutdown."""
    thread = _writer_thread
    if thread is None or not thread.is_alive():
        return True
    timeout = max(timeout, 0.0)
    deadline = time.monotonic() + timeout
    request = _FlushRequest(threading.Event())
    try:
        _write_queue.put(request, timeout=timeout)
    except queue.Full:
        return False
    return request.done.wait(max(deadline - time.monotonic(), 0.0))


def _ensure_writer() -> None:
    global _writer_thread
    with _writer_guard:
        if _writer_thread is not None and _writer_thread.is_alive():
            return
        event_queue = _write_queue
        _writer_thread = threading.Thread(
            target=_writer_loop,
            args=(event_queue,),
            name="dispatch-telemetry",
            daemon=True,
        )
        _writer_thread.start()


def _writer_loop(event_queue: queue.Queue[object]) -> None:
    while True:
        item = event_queue.get()
        if item is _STOP_WRITER:
            return
        if isinstance(item, _FlushRequest):
            item.done.set()
            continue
        if not isinstance(item, _WriteRequest):
            continue
        try:
            _append_line(item.private_path, item.line, create_parents=True, private=True)
            _append_line(item.shared_path, item.line, create_parents=True, private=False)
        except Exception:
            logger.debug("telemetry writer failed", exc_info=True)


def _stop_writer_for_tests() -> None:
    global _write_queue, _writer_thread
    with _writer_guard:
        thread = _writer_thread
        event_queue = _write_queue
        if thread is None:
            return
        try:
            event_queue.put_nowait(_STOP_WRITER)
        except queue.Full:
            return
    thread.join(timeout=1)
    with _writer_guard:
        if _writer_thread is thread:
            _writer_thread = None
            _write_queue = queue.Queue(maxsize=_QUEUE_CAPACITY)


def _flush_at_exit() -> None:
    flush(timeout=0.25)


atexit.register(_flush_at_exit)


def who(
    *,
    days: int = 30,
    root: Path | None = None,
    user: str | None = None,
) -> dict[str, Any]:
    """Aggregate distinct users / sessions / launches from JSONL files."""
    events = list(_iter_events(root=root, user=user, days=days))
    by_user: dict[str, dict[str, Any]] = {}
    for item in events:
        name = str(item.get("user") or "")
        bucket = by_user.setdefault(
            name,
            {
                "user": name,
                "sessions": set(),
                "jobs_launched": 0,
                "last_seen": "",
            },
        )
        sid = item.get("session_id")
        if item.get("event") == "session_start" and sid:
            bucket["sessions"].add(sid)
        if item.get("event") == "job_launched":
            bucket["jobs_launched"] += 1
        ts = str(item.get("ts") or "")
        if ts > bucket["last_seen"]:
            bucket["last_seen"] = ts

    users = []
    for bucket in by_user.values():
        users.append(
            {
                "user": bucket["user"],
                "sessions": len(bucket["sessions"]),
                "jobs_launched": bucket["jobs_launched"],
                "last_seen": bucket["last_seen"],
            }
        )
    users.sort(key=lambda row: row["last_seen"], reverse=True)
    return {"days": days, "users": users}


def summary(
    *,
    days: int = 30,
    root: Path | None = None,
    user: str | None = None,
) -> dict[str, Any]:
    """Aggregate screen views, launch mix, and refusal reasons."""
    events = list(_iter_events(root=root, user=user, days=days))
    screens: Counter[str] = Counter()
    launches: Counter[str] = Counter()
    refusals: Counter[str] = Counter()
    sessions = 0
    for item in events:
        name = item.get("event")
        props = item.get("props") if isinstance(item.get("props"), dict) else {}
        if name == "session_start":
            sessions += 1
        elif name == "screen_view":
            screen = str(props.get("screen") or "unknown")
            screens[screen] += 1
        elif name == "job_launched":
            key = f"{props.get('source', '?')}|{props.get('destination', '?')}"
            launches[key] += 1
        elif name == "launch_refused":
            refusals[str(props.get("reason") or "unknown")] += 1
    return {
        "days": days,
        "sessions": sessions,
        "screens": dict(screens),
        "launches": dict(launches),
        "refusals": dict(refusals),
    }


def format_who(report: dict[str, Any]) -> str:
    lines = [f"Dispatch usage — who (last {report['days']} days)", ""]
    users = report.get("users") or []
    if not users:
        lines.append("No telemetry events found.")
        return "\n".join(lines) + "\n"
    lines.append(f"{'user':<24} {'sessions':>8} {'jobs':>8}  last_seen")
    lines.append("-" * 64)
    for row in users:
        lines.append(
            f"{row['user']:<24} {row['sessions']:>8} {row['jobs_launched']:>8}  {row['last_seen']}"
        )
    return "\n".join(lines) + "\n"


def format_summary(report: dict[str, Any]) -> str:
    lines = [
        f"Dispatch usage — how (last {report['days']} days)",
        f"sessions: {report.get('sessions', 0)}",
        "",
        "screens:",
    ]
    screens = report.get("screens") or {}
    if screens:
        for key, count in sorted(screens.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {key}: {count}")
    else:
        lines.append("  (none)")
    lines.append("launches (source|destination):")
    launches = report.get("launches") or {}
    if launches:
        for key, count in sorted(launches.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {key}: {count}")
    else:
        lines.append("  (none)")
    lines.append("launch refusals:")
    refusals = report.get("refusals") or {}
    if refusals:
        for key, count in sorted(refusals.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {key}: {count}")
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_line(path: Path, line: str, *, create_parents: bool, private: bool) -> None:
    try:
        if create_parents:
            if private:
                config.ensure_private_dir(path.parent)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
        with _open_append(path, private=private) as handle:
            if not _try_lock(handle):
                return
            try:
                handle.write(line)
                handle.flush()
            finally:
                _unlock(handle)
    except OSError:
        logger.debug("telemetry append failed for %s", path, exc_info=True)


def _open_append(path: Path, *, private: bool) -> TextIO:
    if private:
        handle = path.open("a", encoding="utf-8")
        path.chmod(0o600)
        return handle

    # O_CLOEXEC/O_NONBLOCK/O_NOFOLLOW and the euid ownership check are POSIX
    # hardening for the shared Edge Node rollup; on Windows (operator test
    # runs) those APIs do not exist and the shared dir is a per-test tmp path.
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    for flag_name in ("O_CLOEXEC", "O_NONBLOCK", "O_NOFOLLOW"):
        flags |= getattr(os, flag_name, 0)
    fd = os.open(path, flags, 0o644)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"unsafe shared telemetry target: {path}")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise PermissionError(f"unsafe shared telemetry target: {path}")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o644)
        return os.fdopen(fd, "a", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def _try_lock(handle: TextIO) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _unlock(handle: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:  # already released; unlock must never raise into emit paths
        pass


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iter_events(
    *,
    root: Path | None,
    user: str | None,
    days: int,
) -> Any:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 0))
    for path in _event_files(root=root, user=user):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            ts = _parse_ts(str(item.get("ts") or ""))
            if ts is None or ts < cutoff:
                continue
            if user and item.get("user") != user:
                continue
            yield item


def _event_files(*, root: Path | None, user: str | None) -> list[Path]:
    if root is None:
        shared = shared_telemetry_dir()
        private = private_events_path()
        shared_paths: list[Path] = []
        if shared.exists():
            shared_paths = _files_under_shared(shared, user)
        if shared_paths:
            return shared_paths
        if private.exists() and (user is None or user == config.current_user()):
            return [private]
        return []

    return _files_under_shared(root, user)


def _files_under_shared(root: Path, user: str | None) -> list[Path]:
    users_dir = root / "users"
    if user:
        path = users_dir / f"{user}.jsonl"
        return [path] if path.exists() else []
    if not users_dir.is_dir():
        # Allow pointing --dir at a single events.jsonl parent or the file itself.
        if root.is_file():
            return [root]
        direct = root / "events.jsonl"
        return [direct] if direct.exists() else []
    return sorted(users_dir.glob("*.jsonl"))

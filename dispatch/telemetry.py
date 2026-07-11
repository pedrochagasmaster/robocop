"""Offline usage telemetry: who uses Dispatch and how.

Append-only JSONL events to a private per-user log and, when writable, a
shared Edge Node rollup under ``/ads_storage/dispatch/telemetry``. Never raises
into product paths. Opt out with ``DISPATCH_TELEMETRY=0``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config
from .version import __version__

logger = logging.getLogger("dispatch.telemetry")

_FALSEY = {"0", "false", "off", "no"}
_session_id: str | None = None
_session_started_at: datetime | None = None

DEFAULT_SHARED_DIR = Path("/ads_storage/dispatch/telemetry")


def reset_session_for_tests() -> None:
    """Clear process-local session state (tests only)."""
    global _session_id, _session_started_at
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
    return shared_telemetry_dir() / "users" / f"{name}.jsonl"


def emit(event: str, props: dict[str, Any] | None = None, *, user: str | None = None) -> None:
    """Append one telemetry event. Best-effort; never raises to callers."""
    if not enabled():
        return
    try:
        record = {
            "ts": _now_utc(),
            "event": event,
            "user": user or config.current_user(),
            "session_id": session_id(),
            "version": __version__,
            "props": dict(props or {}),
        }
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        _append_line(private_events_path(user), line, create_parents=True, private=True)
        _append_line(shared_user_events_path(user), line, create_parents=True, private=False)
    except Exception:
        logger.debug("telemetry emit failed for %s", event, exc_info=True)


def note_session_start(*, cwd: Path | None = None) -> None:
    global _session_started_at
    _session_started_at = datetime.now(timezone.utc)
    props: dict[str, Any] = {}
    if cwd is not None:
        props["cwd_basename"] = cwd.name or str(cwd)
    emit("session_start", props)


def note_session_end() -> None:
    props: dict[str, Any] = {}
    if _session_started_at is not None:
        props["duration_s"] = int(
            (datetime.now(timezone.utc) - _session_started_at).total_seconds()
        )
    emit("session_end", props)


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
        with path.open("a", encoding="utf-8") as handle:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.write(line)
                    handle.flush()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:
                handle.write(line)
                handle.flush()
        if private:
            try:
                path.chmod(0o600)
            except OSError:
                pass
    except OSError:
        logger.debug("telemetry append failed for %s", path, exc_info=True)


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
        paths: list[Path] = []
        if shared.exists():
            paths.extend(_files_under_shared(shared, user))
        if private.exists() and (user is None or user == config.current_user()):
            paths.append(private)
        # Deduplicate by resolved path while preserving order.
        seen: set[Path] = set()
        unique: list[Path] = []
        for path in paths:
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

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

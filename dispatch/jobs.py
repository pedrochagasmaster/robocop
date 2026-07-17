"""Job directory queries and lifecycle helpers."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import capacity, config, manifest

logger = logging.getLogger("dispatch.jobs")

ACTIVE_WINDOW = timedelta(days=7)
RUNNING_CAP = 2
LAUNCH_SLOT_STATES = {"Pending", "Running"}
PENDING_ORPHAN_GRACE = timedelta(minutes=5)
LAUNCH_WAIT_TIMEOUT_SECONDS = 30.0
LAUNCH_RETRY_SECONDS = 0.25

_manifest_cache: dict[Path, tuple[float, manifest.JobManifest]] = {}


LaunchSlotUnavailable = capacity.CapacityBusy


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _load_manifest_cached(path: Path) -> manifest.JobManifest:
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    cached = _manifest_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    loaded = manifest.load(path)
    _manifest_cache[path] = (mtime, loaded)
    return loaded


def _cached_terminal_outside_active_window(path: Path, now: datetime) -> bool:
    """Return whether an unchanged cached manifest can be skipped by Overview.

    The active dashboard only needs Running/Pending Jobs plus terminal Jobs
    inside the seven-day supervision window. Once a terminal manifest is cached
    and known to be older than that window, each refresh only stats the file to
    detect changes rather than reparsing JSON or reconciling process state.
    """
    cached = _manifest_cache.get(path)
    if cached is None:
        return False
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    cached_mtime, item = cached
    if cached_mtime != mtime or item["state"] in LAUNCH_SLOT_STATES:
        return False
    finished = parse_time(item["finished_at"])
    return finished is not None and now - finished > ACTIVE_WINDOW


def _manifest_paths(root: Path | None = None) -> list[Path]:
    base = root or config.jobs_dir()
    if not base.exists():
        return []
    return sorted(base.glob("*/manifest.json"), reverse=True)


def _prune_manifest_cache(paths: list[Path]) -> None:
    # Drop cache entries for deleted job dirs so the cache cannot grow
    # unbounded across a long supervision session.
    if len(_manifest_cache) > len(paths):
        live = set(paths)
        for stale in [cached for cached in _manifest_cache if cached not in live]:
            del _manifest_cache[stale]


def list_manifests(root: Path | None = None) -> list[manifest.JobManifest]:
    paths = _manifest_paths(root)
    loaded: list[manifest.JobManifest] = []
    for path in paths:
        try:
            loaded.append(_load_manifest_cached(path))
        except Exception as exc:
            logger.warning("Skipping corrupt manifest %s: %s", path, exc)
            continue
    _prune_manifest_cache(paths)
    return loaded


def pid_is_alive(pid: int) -> bool:
    """Return whether ``pid`` still names a live process.

    ``os.kill(pid, 0)`` performs the conservative POSIX liveness probe without
    sending a signal. A permission failure means a process exists but cannot be
    signalled by this user, so it is treated as alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _append_dispatch_log(job_dir: Path, line: str) -> None:
    log_path = job_dir / "run.log"
    if not log_path.exists():
        return
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
    except OSError:
        logger.info("Could not append dispatch note for %s", job_dir)


def reconcile_manifest(path: Path) -> manifest.JobManifest | None:
    """Load and conservatively reconcile one manifest.

    ``Running`` Jobs with a stored PID are failed when the PID no longer
    exists. ``Pending`` Jobs with ``pid=None`` are failed only after the
    manifest file has remained untouched past ``PENDING_ORPHAN_GRACE``.
    """
    item = _load_manifest_cached(path)
    pid = item.get("pid")
    if item["state"] != "Running" or pid is None or pid_is_alive(pid):
        if item["state"] != "Pending" or pid is not None:
            return item
        try:
            manifest_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError as exc:
            raise ValueError(str(exc)) from exc
        now = datetime.now(timezone.utc)
        if now - manifest_mtime <= PENDING_ORPHAN_GRACE:
            return item
        updated = manifest.update(
            path,
            state="Failed",
            exit_code=-1,
            finished_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        _manifest_cache.pop(path, None)
        _append_dispatch_log(
            path.parent,
            "[dispatch] Pending job exceeded startup grace; manifest marked Failed",
        )
        return updated
    updated = manifest.update(
        path,
        state="Failed",
        exit_code=-1,
        finished_at=manifest.now_utc(),
    )
    _manifest_cache.pop(path, None)
    _append_dispatch_log(
        path.parent,
        f"[dispatch] stale runner pid {pid} not found; manifest marked Failed",
    )
    return updated


def reconciled_list_manifests(root: Path | None = None) -> list[manifest.JobManifest]:
    paths = _manifest_paths(root)
    loaded: list[manifest.JobManifest] = []
    for path in paths:
        try:
            loaded.append(reconcile_manifest(path) or _load_manifest_cached(path))
        except Exception as exc:
            logger.warning("Skipping corrupt manifest %s: %s", path, exc)
            continue
    _prune_manifest_cache(paths)
    return loaded


def running_jobs(root: Path | None = None) -> list[manifest.JobManifest]:
    return [item for item in reconciled_list_manifests(root) if item["state"] == "Running"]


def launch_slot_jobs(root: Path | None = None) -> list[manifest.JobManifest]:
    """Return up to ``RUNNING_CAP`` Jobs that currently occupy launch slots.

    ``Pending`` manifests have passed launch acceptance and are waiting for the
    detached runner to flip them to ``Running``, so they count against the cap.
    """
    paths = _manifest_paths(root)
    loaded: list[manifest.JobManifest] = []
    for path in paths:
        try:
            item = reconcile_manifest(path) or _load_manifest_cached(path)
        except Exception as exc:
            logger.warning("Skipping corrupt manifest %s: %s", path, exc)
            continue
        if item["state"] in LAUNCH_SLOT_STATES:
            loaded.append(item)
            if len(loaded) >= RUNNING_CAP:
                break
    _prune_manifest_cache(paths)
    return loaded


def count_launch_slot_jobs(root: Path | None = None) -> int:
    count = 0
    paths = _manifest_paths(root)
    for path in paths:
        try:
            item = reconcile_manifest(path) or _load_manifest_cached(path)
        except Exception as exc:
            logger.warning("Skipping corrupt manifest %s: %s", path, exc)
            continue
        if item["state"] in LAUNCH_SLOT_STATES:
            count += 1
    _prune_manifest_cache(paths)
    return count


def can_launch(root: Path | None = None) -> bool:
    return count_launch_slot_jobs(root) < RUNNING_CAP


def create_job_if_slot_available(
    source: manifest.Source,
    destination: manifest.Destination,
    params: dict[str, Any],
    launch_cwd: Path,
    sql_text: str = "",
    user: str | None = None,
    timeout: float = LAUNCH_WAIT_TIMEOUT_SECONDS,
) -> tuple[Path, manifest.JobManifest]:
    """Atomically admit and create one Pending Job through shared capacity."""

    def create_pending() -> tuple[Path, manifest.JobManifest]:
        return manifest.create_job(
            source=source,
            destination=destination,
            params=params,
            launch_cwd=launch_cwd,
            sql_text=sql_text,
            user=user,
        )

    return capacity.admit_launch(
        create_pending,
        timeout=timeout,
        root=config.data_root(user),
    )


async def create_job_when_capacity_available(
    source: manifest.Source,
    destination: manifest.Destination,
    params: dict[str, Any],
    launch_cwd: Path,
    sql_text: str = "",
    user: str | None = None,
    timeout: float = LAUNCH_WAIT_TIMEOUT_SECONDS,
) -> tuple[Path, manifest.JobManifest]:
    """Wait asynchronously for launch admission without blocking the TUI.

    Each blocking shared-ledger attempt lasts at most 250ms. Cancellation
    shields the active attempt long enough for ``admit_launch`` to remove its
    FIFO intent in ``finally`` before cancellation propagates.
    """
    if timeout < 0:
        raise ValueError("timeout must not be negative")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        remaining = max(0.0, deadline - loop.time())
        attempt_timeout = min(LAUNCH_RETRY_SECONDS, remaining)
        attempt = asyncio.create_task(
            asyncio.to_thread(
                create_job_if_slot_available,
                source=source,
                destination=destination,
                params=params,
                launch_cwd=launch_cwd,
                sql_text=sql_text,
                user=user,
                timeout=attempt_timeout,
            )
        )
        try:
            return await asyncio.shield(attempt)
        except capacity.CapacityTimeout:
            if loop.time() >= deadline:
                raise capacity.CapacityTimeout(
                    f"Dispatch launch capacity timed out after {timeout:g}s"
                ) from None
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(attempt)
            except (asyncio.CancelledError, capacity.CapacityTimeout):
                pass
            raise


def active_jobs(root: Path | None = None) -> list[manifest.JobManifest]:
    now = datetime.now(timezone.utc)
    result = []
    paths = _manifest_paths(root)
    for path in paths:
        try:
            if _cached_terminal_outside_active_window(path, now):
                continue
            item = reconcile_manifest(path) or _load_manifest_cached(path)
        except Exception as exc:
            logger.warning("Skipping corrupt manifest %s: %s", path, exc)
            continue
        finished = parse_time(item["finished_at"])
        if item["state"] == "Running" or finished is None or now - finished <= ACTIVE_WINDOW:
            result.append(item)
    _prune_manifest_cache(paths)
    return result


def history_jobs(root: Path | None = None) -> list[manifest.JobManifest]:
    now = datetime.now(timezone.utc)
    result = []
    paths = _manifest_paths(root)
    for path in paths:
        try:
            if _cached_terminal_outside_active_window(path, now):
                result.append(_manifest_cache[path][1])
                continue
            item = reconcile_manifest(path) or _load_manifest_cached(path)
        except Exception as exc:
            logger.warning("Skipping corrupt manifest %s: %s", path, exc)
            continue
        finished = parse_time(item["finished_at"])
        if finished is not None and now - finished > ACTIVE_WINDOW:
            result.append(item)
    _prune_manifest_cache(paths)
    return result

"""Job directory queries and lifecycle helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

from . import capacity, config, job_lifecycle, manifest

logger = logging.getLogger("dispatch.jobs")

ACTIVE_WINDOW = timedelta(days=7)
RUNNING_CAP = 2
LAUNCH_SLOT_STATES = {"Pending", "Running"}
PENDING_ORPHAN_GRACE = job_lifecycle.PENDING_ORPHAN_GRACE
LAUNCH_WAIT_TIMEOUT_SECONDS = 30.0

_manifest_cache: dict[Path, tuple[float, manifest.JobManifest]] = {}


LaunchSlotUnavailable = capacity.CapacityBusy
pid_is_alive = job_lifecycle.pid_is_alive


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
    """Load one manifest and persist the shared stale-Job transition."""
    item = _load_manifest_cached(path)
    modified_at: datetime | None = None
    if item["state"] == "Pending" and item.get("pid") is None:
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError as exc:
            raise ValueError(str(exc)) from exc
    reconciliation = job_lifecycle.reconcile(item, modified_at, pid_probe=pid_is_alive)
    if reconciliation is None:
        return item

    updated, reconciliation_applied = manifest.update_if_current(
        path,
        item,
        state=reconciliation.manifest["state"],
        exit_code=reconciliation.manifest["exit_code"],
        finished_at=reconciliation.manifest["finished_at"],
    )
    _manifest_cache.pop(path, None)
    if reconciliation_applied:
        _append_dispatch_log(path.parent, reconciliation.log_message)
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


def _pending_job_creator(
    source: manifest.Source,
    destination: manifest.Destination,
    params: dict[str, Any],
    launch_cwd: Path,
    sql_text: str,
    user: str | None,
) -> Callable[[], tuple[Path, manifest.JobManifest]]:
    return partial(
        manifest.create_job,
        source=source,
        destination=destination,
        params=params,
        launch_cwd=launch_cwd,
        sql_text=sql_text,
        user=user,
    )


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
    return capacity.admit_launch(
        _pending_job_creator(source, destination, params, launch_cwd, sql_text, user),
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
    """Wait asynchronously for one atomic Pending admission.

    The capacity module owns FIFO identity, 250ms waits, deadline enforcement,
    and the cancellation-safe callback commit boundary.
    """
    return await capacity.admit_launch_async(
        _pending_job_creator(source, destination, params, launch_cwd, sql_text, user),
        timeout=timeout,
        root=config.data_root(user),
    )


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

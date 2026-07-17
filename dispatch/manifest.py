"""Job manifest schema and helpers."""
# pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from . import config, sql

SourceType = Literal["SqlFile", "SqlTemplate", "ExistingTable"]
DestinationType = Literal["Table", "Csv", "Table+Csv"]
JobState = Literal["Pending", "Running", "Succeeded", "Failed", "Cancelled"]


class Source(TypedDict, total=False):
    type: SourceType
    sql_path_at_launch: str
    table_name: str


class Destination(TypedDict, total=False):
    type: DestinationType
    schema: str
    table_name: str
    csv_path: str


class OrchestratorCall(TypedDict):
    script: str
    argv: list[str]


class JobManifest(TypedDict):
    schema_version: int
    id: str
    tool: str
    user: str
    source: Source
    destination: Destination
    params: dict[str, Any]
    orchestrator_calls: list[OrchestratorCall]
    state: JobState
    pid: int | None
    started_at: str | None
    finished_at: str | None
    exit_code: int | None


LEGAL_CELLS: set[tuple[SourceType, DestinationType]] = {
    ("SqlFile", "Table"),
    ("SqlFile", "Csv"),
    ("SqlFile", "Table+Csv"),
    ("SqlTemplate", "Table"),
    ("ExistingTable", "Csv"),
}

SOURCE_DISPLAY_LABELS: dict[SourceType, str] = {
    "SqlFile": "SqlFile",
    "SqlTemplate": "MonthlyJob",
    "ExistingTable": "ExistingTable",
}


def source_display_label(source_type: SourceType | str) -> str:
    """User-facing source name for the New Job flow (internal types unchanged)."""
    if source_type in SOURCE_DISPLAY_LABELS:
        return SOURCE_DISPLAY_LABELS[source_type]
    return str(source_type)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_job_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = base64.b32encode(os.urandom(5)).decode("ascii").lower()[:6]
    return f"{timestamp}_{token}"


def load(path: Path) -> JobManifest:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    validate(data)
    return data


def _lock_manifest(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_manifest(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _manifest_write_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(".manifest.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"manifest lock is not a regular file: {lock_path}")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            descriptor = -1
            if metadata.st_size == 0:
                handle.write(b"\0")
                handle.flush()
            _lock_manifest(handle)
            try:
                yield
            finally:
                _unlock_manifest(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_unlocked(path: Path, item: JobManifest) -> None:
    validate(item)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(item, handle, indent=2, sort_keys=True)
        handle.write("\n")
    _replace_manifest(tmp, path)


def write(path: Path, item: JobManifest) -> None:
    with _manifest_write_lock(path):
        _write_unlocked(path, item)


def write_if_unchanged(
    path: Path,
    item: JobManifest,
    loaded_metadata: os.stat_result,
) -> bool:
    """Atomically replace ``path`` only while its loaded snapshot is current."""
    with _manifest_write_lock(path):
        try:
            current_metadata = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(current_metadata.st_mode):
            raise ValueError(f"manifest is not a regular file: {path}")
        current_snapshot = (
            current_metadata.st_dev,
            current_metadata.st_ino,
            current_metadata.st_mtime_ns,
        )
        loaded_snapshot = (
            loaded_metadata.st_dev,
            loaded_metadata.st_ino,
            loaded_metadata.st_mtime_ns,
        )
        if current_snapshot != loaded_snapshot:
            return False
        _write_unlocked(path, item)
        return True


def _replace_manifest(tmp: Path, path: Path) -> None:
    """Replace manifest atomically, tolerating transient Windows file locks."""
    delays = (0.02, 0.05, 0.1)
    for delay in delays:
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(delay)
    tmp.replace(path)


def update(path: Path, **changes: Any) -> JobManifest:
    with _manifest_write_lock(path):
        item = load(path)
        item.update(changes)
        _write_unlocked(path, item)
        return item


def update_if_current(
    path: Path,
    expected: JobManifest,
    **changes: Any,
) -> tuple[JobManifest, bool]:
    """Apply changes only if the manifest still matches the loaded snapshot."""
    with _manifest_write_lock(path):
        current = load(path)
        if current != expected:
            return current, False
        current.update(changes)
        _write_unlocked(path, current)
        return current, True


def validate(data: Any) -> None:
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    required = {
        "schema_version",
        "id",
        "tool",
        "user",
        "source",
        "destination",
        "params",
        "orchestrator_calls",
        "state",
        "pid",
        "started_at",
        "finished_at",
        "exit_code",
    }
    missing = required - set(data)
    if missing:
        raise ValueError(f"manifest missing keys: {sorted(missing)}")
    if data["schema_version"] != 1:
        raise ValueError("unsupported manifest schema_version")
    if data["tool"] != "dispatch":
        raise ValueError("manifest tool must be dispatch")
    source_type = data["source"].get("type")
    destination_type = data["destination"].get("type")
    if (source_type, destination_type) not in LEGAL_CELLS:
        raise ValueError(f"illegal Source/Destination cell: {source_type}/{destination_type}")
    if data["state"] not in {"Pending", "Running", "Succeeded", "Failed", "Cancelled"}:
        raise ValueError("invalid Job state")
    if not isinstance(data["orchestrator_calls"], list) or not data["orchestrator_calls"]:
        raise ValueError("manifest requires at least one orchestrator call")


def _effective_job_sql(
    source: Source,
    destination: Destination,
    sql_text: str,
    user: str,
) -> str:
    """The SQL actually written to ``job.sql`` and run verbatim by the orchestrator.

    ``Query_Impala_Parametrized.py`` executes ``job.sql`` as-is and treats
    ``--table-name`` as informational only, so a bare ``SELECT`` would run and
    create nothing. For a ``SqlFile`` job whose destination is a table, wrap the
    SELECT in the auto-generated ``DROP/CREATE TABLE ... STORED AS PARQUET
    LOCATION ... AS`` DDL (the same wrapper the Preview screen shows) so the
    launched job genuinely materializes the table. Other cells run their source
    unchanged: ``Csv`` exports query results via ``download_to_csv.py``,
    ``SqlTemplate`` is wrapped by ``monthly_query_processor.py`` itself, and
    ``ExistingTable`` carries no SQL. A SqlFile that already opens with its own
    ``CREATE``/``INSERT`` DDL is written verbatim to avoid invalid nested DDL.
    """
    if (
        source.get("type") == "SqlFile"
        and destination.get("type") in ("Table", "Table+Csv")
        and not sql.is_self_contained_ddl(sql_text)
    ):
        return sql.table_wrapper(
            sql_text,
            destination.get("schema", ""),
            destination.get("table_name", ""),
            user,
        )
    return sql_text


def create_job(
    source: Source,
    destination: Destination,
    params: dict[str, Any],
    launch_cwd: Path,
    sql_text: str = "",
    user: str | None = None,
) -> tuple[Path, JobManifest]:
    job_user = user or config.current_user()
    job_id = new_job_id()
    home_dir = config.ensure_private_dir(config.dispatch_home(job_user))
    jobs_root = config.ensure_private_dir(home_dir / "jobs")
    job_dir = config.ensure_private_dir(jobs_root / job_id)
    (job_dir / "job.sql").write_text(
        _effective_job_sql(source, destination, sql_text, job_user), encoding="utf-8"
    )
    manifest: JobManifest = {
        "schema_version": 1,
        "id": job_id,
        "tool": "dispatch",
        "user": job_user,
        "source": source,
        "destination": destination,
        "params": params,
        "orchestrator_calls": build_orchestrator_calls(
            job_dir, source, destination, params, launch_cwd, job_user
        ),
        "state": "Pending",
        "pid": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
    }
    write(job_dir / "manifest.json", manifest)
    return job_dir, manifest


def _has_shebang(path: Path) -> bool:
    """True when the file begins with a ``#!`` interpreter line.

    The scr/ orchestrators are marked executable on the edge node but start
    with ``# flake8: noqa`` rather than a shebang, so exec'ing them directly
    fails with ENOEXEC ("Exec format error"). Only trust the executable bit
    when a real shebang is present.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def script_argv(script: str) -> list[str]:
    scr_dir = Path(os.environ.get("DISPATCH_SCR_DIR", "/ads_storage/dispatch/scr"))
    script_path = scr_dir / script
    if script_path.exists() and os.access(script_path, os.X_OK) and _has_shebang(script_path):
        return [str(script_path)]
    # The orchestrators use 3.10+ syntax (PEP 604 ``X | None``). A bare
    # ``python3`` on the edge node can be 3.9, which fails at import time, so
    # prefer an explicit 3.10+ launcher and then this process's own interpreter
    # (the Dispatch venv, known to satisfy the floor) before a bare python3.
    python = (
        shutil.which("python3.10")
        or shutil.which("python3.11")
        or shutil.which("python3.12")
        or sys.executable
        or shutil.which("python3")
    )
    return [python, str(script_path)]


def _csv_path_for_destination(destination: Destination, launch_cwd: Path, table: str) -> str:
    """Return a CSV path confined to ``launch_cwd``.

    Normal TUI launches provide a validated absolute path, but hand-edited
    manifests can carry an explicit ``destination["csv_path"]``. Re-check it
    here before building ``--output-file`` so detached exports cannot write
    outside the launch-time working directory.
    """
    explicit_csv_path = destination.get("csv_path")
    if not explicit_csv_path:
        return str(sql.safe_csv_path(launch_cwd, table or "dispatch_export"))
    return str(sql.resolve_csv_output_path(launch_cwd, explicit_csv_path))


def build_orchestrator_calls(
    job_dir: Path,
    source: Source,
    destination: Destination,
    params: dict[str, Any],
    launch_cwd: Path,
    user: str,
) -> list[OrchestratorCall]:
    source_type = source["type"]
    destination_type = destination["type"]
    if (source_type, destination_type) not in LEGAL_CELLS:
        raise ValueError(f"illegal Source/Destination cell: {source_type}/{destination_type}")

    schema = destination.get("schema", "")
    table = destination.get("table_name", "")
    full_table = f"{schema}.{table}" if schema and "." not in table else table
    if destination_type in {"Table", "Table+Csv"}:
        for value, label in ((schema, "Schema"), (table, "Table name")):
            identifier_error = sql.validate_identifier(value, label)
            if identifier_error:
                raise ValueError(identifier_error)
        full_table_error = sql.validate_full_table(full_table, "Destination table")
        if full_table_error:
            raise ValueError(full_table_error)
    if source_type == "ExistingTable":
        full_table = source.get("table_name") or full_table
        full_table_error = sql.validate_full_table(full_table, "Existing table")
        if full_table_error:
            raise ValueError(full_table_error)
        schema, table = full_table.split(".", 1)
    csv_path = _csv_path_for_destination(destination, launch_cwd, table)
    email = str(params.get("to_email", ""))
    subject = str(params.get("subject", "Dispatch Job"))
    calls: list[OrchestratorCall] = []

    if source_type == "SqlFile" and destination_type in {"Table", "Table+Csv"}:
        argv = script_argv("Query_Impala_Parametrized.py") + [
            "--sql-file",
            str(job_dir / "job.sql"),
            "--table-name",
            full_table,
            "--to-email",
            email,
            "--subject",
            subject,
            "--user",
            user,
            "--session-folder",
            str(job_dir),
        ]
        calls.append({"script": "Query_Impala_Parametrized.py", "argv": argv})

    if source_type == "SqlFile" and destination_type == "Csv":
        argv = script_argv("download_to_csv.py") + [
            "--query-file",
            str(job_dir / "job.sql"),
            "--output-file",
            csv_path,
            "--to-email",
            email,
            "--subject",
            subject,
        ]
        calls.append({"script": "download_to_csv.py", "argv": argv})

    if source_type == "SqlFile" and destination_type == "Table+Csv":
        argv = script_argv("download_to_csv.py") + [
            "--table-name",
            full_table,
            "--output-file",
            csv_path,
            "--to-email",
            email,
            "--subject",
            subject,
        ]
        calls.append({"script": "download_to_csv.py", "argv": argv})

    if source_type == "SqlTemplate":
        argv = script_argv("monthly_query_processor.py") + [
            "--sql-file",
            str(job_dir / "job.sql"),
            "--schema",
            schema,
            "--table-name",
            table,
            "--start-date",
            str(params["start_date"]),
            "--end-date",
            str(params["end_date"]),
            "--user",
            user,
            "--to-email",
            email,
            "--subject",
            subject,
        ]
        calls.append({"script": "monthly_query_processor.py", "argv": argv})

    if source_type == "ExistingTable":
        argv = script_argv("download_to_csv.py") + [
            "--table-name",
            full_table,
            "--output-file",
            csv_path,
            "--to-email",
            email,
            "--subject",
            subject,
        ]
        calls.append({"script": "download_to_csv.py", "argv": argv})

    destination["csv_path"] = csv_path
    return calls

"""Shared launch and supervision operations for TUI and CLI adapters.

Deep module: validation, capacity admission, runner handoff, cancellation,
reconciliation-backed queries, log reading, and wait polling live here.
Adapters own prompts, rendering, and argparse/exit-code presentation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import capacity, config, jobs, kerberos, manifest, process, sql, telemetry
from .advisor import analyze
from .advisor.models import AnalysisResult
from .asyncio_utils import await_uncancellable

logger = logging.getLogger("dispatch.job_ops")

QUEUE_AUTO = "auto"
QUEUE_ORDER = ["adhoc_fast", "adhoc_small", "acs_small", "acs_large", "adhoc"]
QUEUE_VALUES = frozenset(QUEUE_ORDER)

TERMINAL_STATES = frozenset({"Succeeded", "Failed", "Cancelled"})
CANCELLABLE_STATES = frozenset({"Pending", "Running"})

_JOB_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_[a-z0-9]+$")


class JobOpsError(Exception):
    """Base error for shared job operations."""


class ValidationError(JobOpsError):
    """Invalid inputs or launch validation failure."""


class ConfirmationRequired(JobOpsError):
    """An explicit confirmation flag is required before mutating state."""


class AdvisorAcknowledgementRequired(JobOpsError):
    """Advisor error findings require an explicit acknowledgement flag."""

    def __init__(self, message: str, *, analysis: AnalysisResult) -> None:
        super().__init__(message)
        self.analysis = analysis


class UnknownJobError(JobOpsError):
    """Job ID is missing, malformed, or outside the jobs root."""


class OperationalError(JobOpsError):
    """Kerberos, capacity, handoff, timeout, or cancellation failure."""


CancelKind = Literal["pending_cancelled", "signaled", "reconciled_missing", "not_cancellable"]


@dataclass(frozen=True)
class LaunchInputs:
    """Non-interactive representation of the New Job form."""

    source_type: manifest.SourceType
    destination_type: manifest.DestinationType
    launch_cwd: Path
    sql_path: str = ""
    existing_table: str = ""
    schema: str = "aa_enc"
    table_name: str = ""
    start_date: str = ""
    end_date: str = ""
    email: str = ""
    subject: str = "Dispatch Job"
    queue: str = QUEUE_AUTO
    user: str | None = None


@dataclass(frozen=True)
class LaunchPlan:
    """Validated launch payload ready for confirmation and admission."""

    inputs: LaunchInputs
    source: manifest.Source
    destination: manifest.Destination
    params: dict[str, str]
    sql_text: str
    analysis: AnalysisResult
    resolved_sql_path: Path | None


@dataclass(frozen=True)
class LaunchResult:
    job_id: str
    job_dir: Path
    manifest: manifest.JobManifest
    handoff_failed: bool = False
    handoff_error: str | None = None


@dataclass(frozen=True)
class CancelResult:
    job_id: str
    kind: CancelKind
    manifest: manifest.JobManifest
    message: str


@dataclass(frozen=True)
class WaitResult:
    job_id: str
    manifest: manifest.JobManifest
    timed_out: bool


def normalize_queues(raw: str | list[str] | tuple[str, ...] | None) -> str:
    """Serialize queue selection to the manifest ``params.queue`` value."""
    if raw is None:
        return QUEUE_AUTO
    if isinstance(raw, str):
        pieces = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        pieces = [str(part).strip() for part in raw if str(part).strip()]
    if not pieces or (len(pieces) == 1 and pieces[0].lower() == QUEUE_AUTO):
        return QUEUE_AUTO
    unknown = [queue for queue in pieces if queue not in QUEUE_VALUES]
    if unknown:
        raise ValidationError(
            f"Unknown queue(s): {', '.join(unknown)}. "
            f"Choose from: {', '.join(QUEUE_ORDER)} or '{QUEUE_AUTO}'."
        )
    ordered = [queue for queue in QUEUE_ORDER if queue in set(pieces)]
    return ",".join(ordered)


def resolve_sql_path(launch_cwd: Path, raw: str) -> Path:
    """Resolve a SQL path against the launch CWD (invocation CWD for CLI)."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = launch_cwd / path
    return path


def job_created_at(job_id: str) -> str | None:
    """Best-effort creation timestamp embedded in the Job ID."""
    if "_" not in job_id:
        return None
    stamp = job_id.split("_", 1)[0]
    if len(stamp) == 16 and stamp.endswith("Z") and "T" in stamp:
        return stamp
    return None


def resolve_job_dir(job_id: str, root: Path | None = None) -> Path:
    """Resolve ``job_id`` to a directory under the jobs root, safely.

    Rejects empty/malformed IDs, path separators, symlink-escaped directories,
    and paths that resolve outside the configured jobs root.
    """
    if not job_id or not isinstance(job_id, str):
        raise UnknownJobError("Job ID is required")
    if job_id in {".", ".."} or "/" in job_id or "\\" in job_id or "\x00" in job_id:
        raise UnknownJobError(f"Malformed Job ID: {job_id}")
    if not _JOB_ID_RE.fullmatch(job_id):
        raise UnknownJobError(f"Malformed Job ID: {job_id}")

    jobs_root = (root or config.jobs_dir()).resolve(strict=False)
    candidate = jobs_root / job_id
    try:
        meta = candidate.lstat()
    except FileNotFoundError as exc:
        raise UnknownJobError(f"Unknown Job ID: {job_id}") from exc
    except OSError as exc:
        raise OperationalError(f"Cannot inspect Job path {candidate}: {exc}") from exc
    if stat.S_ISLNK(meta.st_mode):
        raise UnknownJobError(f"Unsafe Job path (symlink): {job_id}")
    if not stat.S_ISDIR(meta.st_mode):
        raise UnknownJobError(f"Unknown Job ID: {job_id}")

    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(jobs_root)
    except ValueError as exc:
        raise UnknownJobError(f"Unsafe Job path: {job_id}") from exc
    return resolved


def load_job(job_id: str, root: Path | None = None) -> manifest.JobManifest:
    """Load and reconcile one Job manifest."""
    job_dir = resolve_job_dir(job_id, root=root)
    path = job_dir / "manifest.json"
    try:
        path_meta = path.lstat()
    except FileNotFoundError as exc:
        raise UnknownJobError(f"Unknown Job ID: {job_id}") from exc
    except OSError as exc:
        raise OperationalError(f"Cannot inspect manifest for {job_id}: {exc}") from exc
    if stat.S_ISLNK(path_meta.st_mode):
        raise UnknownJobError(f"Unsafe Job path (symlink): {job_id}")
    try:
        item = jobs.reconcile_manifest(path)
    except Exception as exc:
        raise OperationalError(f"Corrupt or unreadable Job {job_id}: {exc}") from exc
    if item is None:
        raise OperationalError(f"Corrupt or unreadable Job {job_id}")
    return item


def list_jobs(
    *,
    state: str | None = None,
    root: Path | None = None,
) -> list[manifest.JobManifest]:
    """Return reconciled manifests, optionally filtered by state."""
    items = jobs.reconciled_list_manifests(root)
    if state is None:
        return items
    if state not in {"Pending", "Running", "Succeeded", "Failed", "Cancelled"}:
        raise ValidationError(
            f"Unknown state filter: {state}. "
            "Choose Pending, Running, Succeeded, Failed, or Cancelled."
        )
    return [item for item in items if item["state"] == state]


def job_summary_dict(item: manifest.JobManifest) -> dict[str, Any]:
    """Stable summary fields for list/show JSON and human tables."""
    source = item.get("source") or {}
    destination = item.get("destination") or {}
    return {
        "id": item["id"],
        "state": item["state"],
        "source": source.get("type"),
        "destination": destination.get("type"),
        "source_detail": source.get("table_name") or source.get("sql_path_at_launch"),
        "destination_detail": _destination_detail(destination),
        "created_at": job_created_at(item["id"]),
        "started_at": item.get("started_at"),
        "finished_at": item.get("finished_at"),
        "pid": item.get("pid"),
        "exit_code": item.get("exit_code"),
        "user": item.get("user"),
        "params": item.get("params") or {},
    }


def job_detail_dict(item: manifest.JobManifest) -> dict[str, Any]:
    """Complete useful status for ``job show``."""
    detail = job_summary_dict(item)
    detail["manifest"] = dict(item)
    return detail


def _destination_detail(destination: dict[str, Any]) -> str | None:
    dest_type = destination.get("type")
    schema = destination.get("schema") or ""
    table = destination.get("table_name") or ""
    csv_path = destination.get("csv_path") or ""
    if dest_type == "Csv":
        return csv_path or None
    if dest_type == "Table":
        return f"{schema}.{table}" if schema and table else table or None
    if dest_type == "Table+Csv":
        table_ref = f"{schema}.{table}" if schema and table else table
        return f"{table_ref} + {csv_path}".strip(" +") or None
    return None


def _refusal_reason(error: str) -> telemetry.RefusalReason:
    lowered = error.lower()
    if "concurrency cap" in lowered or "capacity" in lowered:
        return "slot_cap"
    if "kerberos" in lowered:
        return "kerberos"
    return "validation"


def validation_issues(
    inputs: LaunchInputs,
    *,
    kerberos_ttl: int | None,
    deep: bool = False,
) -> list[str]:
    """Collect launch problems in the same order as the New Job form."""
    issues: list[str] = []
    source = inputs.source_type
    destination = inputs.destination_type
    eid = inputs.user or config.current_user()

    if (source, destination) not in manifest.LEGAL_CELLS:
        issues.append(
            f"Illegal combination: {manifest.source_display_label(source)} → {destination}"
        )

    table_name = inputs.table_name.strip()
    if destination in ("Table", "Table+Csv"):
        schema_error = sql.validate_identifier(inputs.schema, "Schema")
        if schema_error:
            issues.append(schema_error)
        if not table_name:
            issues.append("Table name is required")
        else:
            table_error = sql.validate_eid_table_name(table_name, eid)
            if table_error:
                issues.append(table_error)

    resolved_sql: Path | None = None
    if source in ("SqlFile", "SqlTemplate"):
        if not inputs.sql_path.strip():
            issues.append("SQL file path is required")
        else:
            resolved_sql = resolve_sql_path(inputs.launch_cwd, inputs.sql_path)
            if not resolved_sql.is_file():
                issues.append("SQL file not found")

    if source == "SqlTemplate":
        date_error = sql.validate_date_range(inputs.start_date, inputs.end_date)
        if date_error:
            issues.append(date_error)

    existing_error: str | None = None
    existing = inputs.existing_table.strip()
    if source == "ExistingTable":
        existing_error = sql.validate_full_table(existing, "Existing table")
        if existing_error:
            issues.append(existing_error)

    if destination in ("Csv", "Table+Csv"):
        csv_table = table_name
        if source == "ExistingTable" and existing_error is None and "." in existing:
            _schema, csv_table = existing.split(".", 1)
        if not csv_table:
            issues.append("CSV destination requires a table name stem")
        else:
            try:
                sql.safe_csv_path(inputs.launch_cwd, csv_table)
            except ValueError as exc:
                issues.append(str(exc))

    email = inputs.email.strip()
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        issues.append("Invalid email format")

    try:
        normalize_queues(inputs.queue)
    except ValidationError as exc:
        issues.append(str(exc))

    if kerberos_ttl is None:
        issues.append("Kerberos ticket missing — run kinit")
    elif kerberos_ttl < kerberos.MIN_LAUNCH_TTL_SECONDS:
        issues.append("Kerberos ticket TTL is under 5 minutes — renew with kinit")

    if deep and source != "ExistingTable" and resolved_sql is not None and resolved_sql.is_file():
        issues.extend(_sql_content_issues(source, resolved_sql))
    return issues


def _sql_content_issues(source: str, sql_path: Path) -> list[str]:
    try:
        sql_text = sql_path.read_text(encoding="utf-8")
    except OSError:
        return ["SQL file is unreadable"]
    if sql.is_malformed_template(sql_text):
        return ["SQL contains only one of {date_inicio}/{date_fim} — likely a typo"]
    if source == "SqlTemplate" and not sql.template_is_complete(sql_text):
        return [
            f"{manifest.source_display_label('SqlTemplate')} requires both "
            "{date_inicio} and {date_fim}"
        ]
    return []


def prepare_launch(
    inputs: LaunchInputs,
    *,
    kerberos_ttl: int | None,
) -> LaunchPlan:
    """Validate inputs and build source/destination/params/analysis."""
    issues = validation_issues(inputs, kerberos_ttl=kerberos_ttl, deep=True)
    if issues:
        reason = _refusal_reason(issues[0])
        telemetry.note_launch_refused(reason)
        raise ValidationError(issues[0])

    eid = inputs.user or config.current_user()
    source_type = inputs.source_type
    destination_type = inputs.destination_type
    schema = inputs.schema.strip()
    table = inputs.table_name.strip()
    resolved_sql: Path | None = None
    sql_text = ""

    if source_type == "ExistingTable":
        existing = inputs.existing_table.strip()
        source: manifest.Source = {"type": "ExistingTable", "table_name": existing}
        if "." in existing:
            schema, table = existing.split(".", 1)
    else:
        resolved_sql = resolve_sql_path(inputs.launch_cwd, inputs.sql_path)
        try:
            sql_text = resolved_sql.read_text(encoding="utf-8")
        except OSError as exc:
            telemetry.note_launch_refused("validation")
            raise ValidationError(f"Cannot read SQL file: {resolved_sql}\n{exc}") from exc
        source = {"type": source_type, "sql_path_at_launch": str(resolved_sql)}

    try:
        csv_path = str(sql.safe_csv_path(inputs.launch_cwd, table))
    except ValueError as exc:
        telemetry.note_launch_refused("validation")
        raise ValidationError(str(exc)) from exc

    destination: manifest.Destination = {
        "type": destination_type,
        "schema": schema,
        "table_name": table,
        "csv_path": csv_path,
    }
    params = {
        "to_email": inputs.email.strip(),
        "subject": inputs.subject.strip() or "Dispatch Job",
        "queue": normalize_queues(inputs.queue),
    }
    if source_type == "SqlTemplate":
        params["start_date"] = sql.to_orchestrator_date(inputs.start_date)
        params["end_date"] = sql.to_orchestrator_date(inputs.end_date)

    analysis = analyze(
        sql_text,
        source_type=source_type,
        destination_type=destination_type,
        destination_table=table,
        user_id=eid,
    )
    return LaunchPlan(
        inputs=inputs,
        source=source,
        destination=destination,
        params=params,
        sql_text=sql_text,
        analysis=analysis,
        resolved_sql_path=resolved_sql,
    )


def launch_summary_text(plan: LaunchPlan) -> str:
    """Plain-text launch summary (no Textual markup)."""
    source = plan.source
    destination = plan.destination
    source_type = source["type"]
    source_detail = source.get("table_name") or source.get("sql_path_at_launch") or "--"
    dest_type = destination["type"]
    schema = destination.get("schema") or "--"
    table = destination.get("table_name") or "--"
    csv_path = destination.get("csv_path") or "--"
    queue = plan.params.get("queue", QUEUE_AUTO)
    queue_label = "Auto (cycle all queues)" if queue == QUEUE_AUTO else queue.replace(",", ", ")
    email = plan.params.get("to_email") or "--"
    return (
        f"Source: {manifest.source_display_label(source_type)}  {source_detail}\n"
        f"Destination: {dest_type}\n"
        f"Target table: {schema}.{table}\n"
        f"Queue: {queue_label}\n"
        f"CSV path: {csv_path}\n"
        f"Email: {email}"
    )


def require_launch_confirmation(
    plan: LaunchPlan,
    *,
    yes: bool,
    acknowledge_advisor: bool,
) -> None:
    """Enforce non-interactive confirmation flags for CLI launches."""
    errors = plan.analysis.errors()
    if errors and not acknowledge_advisor:
        names = ", ".join(sorted({finding.rule_id for finding in errors}))
        raise AdvisorAcknowledgementRequired(
            "Advisor reported error-severity findings "
            f"({names}). Re-run with --acknowledge-advisor to launch as written.",
            analysis=plan.analysis,
        )
    if not yes:
        raise ConfirmationRequired(
            "Refusing to launch without --yes. Review the plan, then pass --yes."
        )


def mark_runner_handoff_failed(job_dir: Path, exc: BaseException) -> None:
    """Persist the terminal Failed state used when detached handoff fails."""
    manifest.update(
        job_dir / "manifest.json",
        state="Failed",
        exit_code=-1,
        finished_at=manifest.now_utc(),
    )
    logger.exception("Failed to launch runner for Job %s", job_dir.name)


async def launch_runner_after_commit(job_dir: Path) -> int:
    """Finish runner handoff after Pending commit despite task cancellation."""
    launch = asyncio.create_task(process.launch_runner(job_dir))
    return await await_uncancellable(launch)


async def execute_launch_async(plan: LaunchPlan) -> LaunchResult:
    """Admit capacity, create the Pending manifest, and hand off to the runner."""
    try:
        job_dir, job_manifest = await jobs.create_job_when_capacity_available(
            source=plan.source,
            destination=plan.destination,
            params=plan.params,
            launch_cwd=plan.inputs.launch_cwd,
            sql_text=plan.sql_text,
            user=plan.inputs.user,
        )
    except capacity.CapacityBusy as exc:
        telemetry.note_launch_refused("slot_cap")
        raise OperationalError(str(exc)) from exc
    except (capacity.CapacityTimeout, capacity.CapacityLedgerError) as exc:
        telemetry.note_launch_refused("validation")
        raise OperationalError(str(exc)) from exc

    telemetry.note_job_launched(
        job_id=job_dir.name,
        source=plan.source["type"],
        destination=plan.destination["type"],
    )
    try:
        await launch_runner_after_commit(job_dir)
    except OSError as exc:
        mark_runner_handoff_failed(job_dir, exc)
        failed = manifest.load(job_dir / "manifest.json")
        return LaunchResult(
            job_id=job_dir.name,
            job_dir=job_dir,
            manifest=failed,
            handoff_failed=True,
            handoff_error=f"Could not launch detached runner: {exc}",
        )
    logger.info(
        "Launched Job %s source=%s dest=%s",
        job_dir.name,
        plan.source["type"],
        plan.destination["type"],
    )
    return LaunchResult(job_id=job_dir.name, job_dir=job_dir, manifest=job_manifest)


def execute_launch(plan: LaunchPlan) -> LaunchResult:
    """Synchronous launch path for the CLI adapter."""
    try:
        job_dir, job_manifest = jobs.create_job_if_slot_available(
            source=plan.source,
            destination=plan.destination,
            params=plan.params,
            launch_cwd=plan.inputs.launch_cwd,
            sql_text=plan.sql_text,
            user=plan.inputs.user,
        )
    except capacity.CapacityBusy as exc:
        telemetry.note_launch_refused("slot_cap")
        raise OperationalError(str(exc)) from exc
    except (capacity.CapacityTimeout, capacity.CapacityLedgerError) as exc:
        telemetry.note_launch_refused("validation")
        raise OperationalError(str(exc)) from exc

    telemetry.note_job_launched(
        job_id=job_dir.name,
        source=plan.source["type"],
        destination=plan.destination["type"],
    )
    try:
        process.launch_runner_detached(job_dir)
    except OSError as exc:
        mark_runner_handoff_failed(job_dir, exc)
        failed = manifest.load(job_dir / "manifest.json")
        return LaunchResult(
            job_id=job_dir.name,
            job_dir=job_dir,
            manifest=failed,
            handoff_failed=True,
            handoff_error=f"Could not launch detached runner: {exc}",
        )
    logger.info(
        "Launched Job %s source=%s dest=%s",
        job_dir.name,
        plan.source["type"],
        plan.destination["type"],
    )
    return LaunchResult(job_id=job_dir.name, job_dir=job_dir, manifest=job_manifest)


def launch_job(
    inputs: LaunchInputs,
    *,
    kerberos_ttl: int | None,
    yes: bool = False,
    acknowledge_advisor: bool = False,
) -> LaunchResult:
    """CLI-oriented full launch: prepare, confirm flags, admit, hand off."""
    plan = prepare_launch(inputs, kerberos_ttl=kerberos_ttl)
    require_launch_confirmation(plan, yes=yes, acknowledge_advisor=acknowledge_advisor)
    # Re-check Kerberos freshness is the caller's responsibility for async TUI;
    # CLI passes the TTL measured just before this call.
    issues = validation_issues(inputs, kerberos_ttl=kerberos_ttl, deep=True)
    if issues:
        telemetry.note_launch_refused(_refusal_reason(issues[0]))
        raise ValidationError(issues[0])
    result = execute_launch(plan)
    if result.handoff_failed:
        raise OperationalError(result.handoff_error or "Detached runner handoff failed")
    return result


def cancel_job(job_id: str, *, yes: bool = False, root: Path | None = None) -> CancelResult:
    """Cancel a Pending or Running Job with the TUI's process-group semantics."""
    if not yes:
        raise ConfirmationRequired(
            "Refusing to cancel without --yes. Pass --yes to confirm cancellation."
        )
    job_dir = resolve_job_dir(job_id, root=root)
    path = job_dir / "manifest.json"
    try:
        item = manifest.load(path)
    except Exception as exc:
        raise OperationalError(f"Corrupt or unreadable Job {job_id}: {exc}") from exc

    pid = item.get("pid")
    if item["state"] == "Pending" and not pid:
        updated = manifest.update(
            path,
            state="Cancelled",
            exit_code=0,
            finished_at=manifest.now_utc(),
        )
        telemetry.note_job_cancelled(item["id"])
        return CancelResult(
            job_id=item["id"],
            kind="pending_cancelled",
            manifest=updated,
            message=f"Pending Job {item['id']} cancelled",
        )

    if item["state"] in CANCELLABLE_STATES and pid:
        try:
            result = process.cancel_process_group(pid)
        except ProcessLookupError:
            result = "missing"
        except PermissionError as exc:
            raise OperationalError(
                "Permission denied while signalling the Job process group"
            ) from exc
        if result == "missing":
            reconciled = jobs.reconcile_manifest(path)
            if reconciled is None:
                raise OperationalError(
                    "Job process is no longer running; failed to reconcile manifest"
                )
            return CancelResult(
                job_id=item["id"],
                kind="reconciled_missing",
                manifest=reconciled,
                message="Job process is no longer running; manifest marked Failed",
            )
        telemetry.note_job_cancelled(item["id"])
        current = manifest.load(path)
        return CancelResult(
            job_id=item["id"],
            kind="signaled",
            manifest=current,
            message=f"Cancellation requested for Job {item['id']}",
        )

    return CancelResult(
        job_id=item["id"],
        kind="not_cancellable",
        manifest=item,
        message="No cancellable Job process found",
    )


def read_log_tail(job_id: str, *, lines: int = 50, root: Path | None = None) -> list[str]:
    """Return the last ``lines`` of the Job log (empty if missing)."""
    if lines < 0:
        raise ValidationError("--lines must be >= 0")
    job_dir = resolve_job_dir(job_id, root=root)
    log_path = job_dir / "run.log"
    if not log_path.exists():
        return []
    try:
        meta = log_path.lstat()
    except OSError as exc:
        raise OperationalError(f"Cannot read log for {job_id}: {exc}") from exc
    if stat.S_ISLNK(meta.st_mode):
        raise UnknownJobError(f"Unsafe Job log path (symlink): {job_id}")
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OperationalError(f"Cannot read log for {job_id}: {exc}") from exc
    if lines == 0:
        return []
    all_lines = content.splitlines()
    return all_lines[-lines:]


def follow_logs(
    job_id: str,
    *,
    lines: int = 50,
    poll_interval: float = 0.5,
    root: Path | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[str]:
    """Yield new log lines until the Job is terminal and the log is drained."""
    job_dir = resolve_job_dir(job_id, root=root)
    log_path = job_dir / "run.log"
    # Emit the initial tail first.
    yield from read_log_tail(job_id, lines=lines, root=root)
    offset = 0
    if log_path.exists():
        try:
            offset = log_path.stat().st_size
        except OSError:
            offset = 0
    # If we printed a tail, the file offset is at EOF already for follow.
    # Re-open from EOF so we only stream newly appended bytes.
    pending = b""
    while True:
        if should_stop is not None and should_stop():
            return
        item = load_job(job_id, root=root)
        terminal = item["state"] in TERMINAL_STATES
        try:
            size = log_path.stat().st_size if log_path.exists() else offset
        except OSError:
            size = offset
        if size < offset:
            offset = 0
            pending = b""
        if size > offset and log_path.exists():
            with log_path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
            parts = (pending + chunk).split(b"\n")
            pending = parts.pop()
            for raw in parts:
                yield raw.decode("utf-8", errors="replace").rstrip("\r")
            if terminal and pending:
                yield pending.decode("utf-8", errors="replace").rstrip("\r")
                pending = b""
                return
        elif terminal:
            if pending:
                yield pending.decode("utf-8", errors="replace").rstrip("\r")
            return
        time.sleep(poll_interval)


def wait_job(
    job_id: str,
    *,
    timeout: float | None = None,
    poll_interval: float = 1.0,
    root: Path | None = None,
) -> WaitResult:
    """Poll until the Job reaches a terminal state or ``timeout`` elapses."""
    if poll_interval <= 0:
        raise ValidationError("--poll-interval must be > 0")
    if timeout is not None and timeout < 0:
        raise ValidationError("--timeout must be >= 0")
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        item = load_job(job_id, root=root)
        if item["state"] in TERMINAL_STATES:
            return WaitResult(job_id=job_id, manifest=item, timed_out=False)
        if deadline is not None and time.monotonic() >= deadline:
            return WaitResult(job_id=job_id, manifest=item, timed_out=True)
        sleep_for = poll_interval
        if deadline is not None:
            sleep_for = min(poll_interval, max(0.0, deadline - time.monotonic()))
        time.sleep(sleep_for)


def table_name_for_inputs(
    *,
    source_type: manifest.SourceType,
    destination_type: manifest.DestinationType,
    table_suffix_or_full: str,
    user: str | None = None,
) -> str:
    """Apply the EID table-name prefix the New Job form uses."""
    eid = user or config.current_user()
    suffix = sql.split_eid_table_suffix(table_suffix_or_full.strip(), eid).strip()
    needs_table = destination_type in ("Table", "Table+Csv") or source_type == "SqlTemplate"
    if needs_table:
        return sql.join_eid_table_name(eid, suffix)
    return suffix

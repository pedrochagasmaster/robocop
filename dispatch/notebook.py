"""Notebook API for launching and monitoring Jobs.

Thin adapter over the ``dispatch job`` CLI: every operation runs
``dispatch job … --json`` in a subprocess and parses one JSON document, so
notebooks inherit the CLI's validation, Kerberos checks, Advisor gates,
capacity admission, manifest persistence, and telemetry unchanged. Nothing in
this module reimplements Job behavior, and nothing here imports Textual.

    from dispatch.notebook import Dispatch

    d = Dispatch(cwd="~/sql")
    df = d.sql("SELECT dt, count(*) c FROM aa_enc.events GROUP BY dt").to_df()

    job = d.launch(source="SqlFile", destination="Csv", sql="query.sql", table="report")
    job.watch()          # live state and log tail until the Job is terminal
    job.succeeded        # True / False
    d.jobs()             # every Job, rendered as a table in Jupyter

Inline SQL is written to a Dispatch-owned file in the Notebook workspace and
launched as an ordinary ``SqlFile`` Job, submitted eagerly (ADR-0009). Results
are read back from the CSV the Job wrote, strictly (ADR-0010). There is no fast
unaudited query path (ADR-0011).

Refused commands raise: :class:`UsageError` (invalid inputs, Advisor
acknowledgement), :class:`UnknownJobError`, :class:`OperationalError`
(Kerberos, capacity, handoff). A Job that ran and failed is data, not an
exception, so :meth:`Job.wait` returns the Job in every terminal state; reading
the Result of an unsuccessful Job raises :class:`JobUnsuccessful`.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import config, notebook_display, results
from .results import MissingResultError, ResultError, ResultParseError

__all__ = [
    "CleanupReport",
    "Dispatch",
    "DispatchError",
    "Job",
    "JobList",
    "JobUnsuccessful",
    "MissingResultError",
    "OperationalError",
    "ResultError",
    "ResultParseError",
    "UnknownJobError",
    "UsageError",
    "WaitTimeout",
    "cli_command",
]

SourceType = Literal["SqlFile", "SqlTemplate", "ExistingTable"]
DestinationType = Literal["Table", "Csv", "Table+Csv"]
JobState = Literal["Pending", "Running", "Succeeded", "Failed", "Cancelled"]

TERMINAL_STATES = frozenset({"Succeeded", "Failed", "Cancelled"})

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_LOG_LINES = 50
WATCH_LOG_LINES = 12

#: Notebook Jobs older than this are removed by :meth:`Dispatch.cleanup`,
#: matching the window the dashboard keeps Jobs active for.
DEFAULT_CLEANUP_DAYS = 7.0

#: Override the CLI invocation, e.g. ``DISPATCH_CLI=/ads_storage/dispatch/bin/dispatch``.
CLI_ENV_VAR = "DISPATCH_CLI"

_SLOT_RETRY_INTERVAL = 5.0
_CAPACITY_MARKERS = ("capacity", "concurrency cap")
_FULL_TABLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*")


class DispatchError(Exception):
    """A ``dispatch job`` command was refused or could not be run."""

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str] = (),
        exit_code: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.argv = list(argv)
        self.exit_code = exit_code
        self.stderr = stderr


class UsageError(DispatchError):
    """Invalid inputs, failed launch validation, or a missing Advisor acknowledgement."""


class UnknownJobError(DispatchError):
    """Job ID is unknown, malformed, or unsafe."""


class OperationalError(DispatchError):
    """Kerberos, capacity, handoff, or cancellation failure."""


class JobUnsuccessful(DispatchError):
    """A command reported that the Job completed unsuccessfully."""


class WaitTimeout(DispatchError, TimeoutError):
    """The Job was still running when the wait deadline passed.

    The Job itself is unaffected: runners are detached, so it keeps running and
    can be waited on again.
    """


_EXIT_ERRORS: dict[int, type[DispatchError]] = {
    1: JobUnsuccessful,
    2: UsageError,
    3: UnknownJobError,
    4: OperationalError,
}


def cli_command() -> list[str]:
    """Return the CLI invocation used by default.

    Honors ``$DISPATCH_CLI`` so notebooks running on a kernel without the
    package importable can point at the installed ``dispatch`` launcher.
    """
    override = os.environ.get(CLI_ENV_VAR, "").strip()
    if override:
        return shlex.split(override)
    return [sys.executable, "-m", "dispatch"]


class Dispatch:
    """Launch and query Jobs through the ``dispatch job`` CLI.

    ``cwd`` is the directory the CLI is invoked from, so it decides where
    relative ``sql`` paths resolve and where CSV destinations are written
    (ADR-0003). It defaults to the notebook's working directory.

    ``workspace`` is the Notebook workspace: Inline SQL and the Results of
    :meth:`sql` and :meth:`table` land in a directory per query underneath it,
    never in ``cwd`` (ADR-0010).
    """

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        command: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        workspace: str | os.PathLike[str] | None = None,
    ) -> None:
        self._cwd = Path(cwd).expanduser() if cwd is not None else Path.cwd()
        self._command = list(command) if command is not None else cli_command()
        self._env = dict(env or {})
        self._timeout = timeout
        self._workspace = (
            Path(workspace).expanduser() if workspace is not None else self._default_workspace()
        )

    @property
    def cwd(self) -> Path:
        """Directory the CLI runs in; CSV results land here."""
        return self._cwd

    @property
    def command(self) -> list[str]:
        """CLI invocation this session shells out to."""
        return list(self._command)

    @property
    def workspace(self) -> Path:
        """Notebook workspace holding Inline SQL and Results."""
        return self._workspace

    def sql(
        self,
        text: str,
        *,
        source: SourceType = "SqlFile",
        destination: DestinationType = "Csv",
        table: str | None = None,
        schema: str | None = None,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        email: str | None = None,
        subject: str | None = None,
        queue: str | Sequence[str] | None = None,
        acknowledge_advisor: bool = False,
        wait_for_slot: float | None = None,
    ) -> Job:
        """Launch a Job from SQL written here, and return it (ADR-0009).

        The text is saved as Inline SQL in the Notebook workspace and launched
        like any other ``SqlFile`` Job: same validation, Kerberos check, Advisor
        gate, capacity cap, manifest, and detached runner. The call submits
        immediately; read the Result with :meth:`Job.to_df` or monitor it with
        :meth:`Job.watch`.

        Pass ``source="SqlTemplate"`` with ``start_date``/``end_date`` for
        ``{date_inicio}``/``{date_fim}`` templates, and ``destination="Table"``
        with ``table=`` to materialise a table instead of a Result.
        """
        if not text.strip():
            raise UsageError("SQL text is empty")
        stem = table or _query_stem()
        query_dir = self._new_query_dir(stem)
        sql_path = query_dir / f"{stem}.sql"
        sql_path.write_text(_ensure_trailing_newline(text), encoding="utf-8")
        session = self._at(query_dir)
        try:
            return session.launch(
                source=source,
                destination=destination,
                sql=sql_path,
                schema=schema,
                table=stem,
                start_date=start_date,
                end_date=end_date,
                email=email,
                subject=subject,
                queue=queue,
                acknowledge_advisor=acknowledge_advisor,
                wait_for_slot=wait_for_slot,
            )
        except DispatchError:
            _discard_dir(query_dir)
            raise

    def table(
        self,
        name: str,
        *,
        limit: int | None = None,
        email: str | None = None,
        subject: str | None = None,
        queue: str | Sequence[str] | None = None,
        acknowledge_advisor: bool = False,
        wait_for_slot: float | None = None,
    ) -> Job:
        """Export an existing ``schema.table`` to a Result and return the Job.

        Without ``limit`` this is an ``ExistingTable`` Job and the orchestrator
        exports the whole table. With ``limit`` Dispatch launches
        ``SELECT * FROM <name> LIMIT <limit>`` as Inline SQL, which the Advisor
        analyses like any other SQL (ADR-0009).
        """
        if not _FULL_TABLE_RE.fullmatch(name.strip()):
            raise UsageError(f"Table must be schema.table using plain Impala identifiers: {name!r}")
        target = name.strip()
        if limit is not None:
            if int(limit) <= 0:
                raise UsageError("limit must be a positive number of rows")
            return self.sql(
                f"SELECT * FROM {target} LIMIT {int(limit)}",
                email=email,
                subject=subject,
                queue=queue,
                acknowledge_advisor=acknowledge_advisor,
                wait_for_slot=wait_for_slot,
            )
        query_dir = self._new_query_dir(target.split(".", 1)[1])
        session = self._at(query_dir)
        try:
            return session.launch(
                source="ExistingTable",
                destination="Csv",
                existing_table=target,
                email=email,
                subject=subject,
                queue=queue,
                acknowledge_advisor=acknowledge_advisor,
                wait_for_slot=wait_for_slot,
            )
        except DispatchError:
            _discard_dir(query_dir)
            raise

    def launch(
        self,
        *,
        source: SourceType,
        destination: DestinationType,
        sql: str | os.PathLike[str] | None = None,
        existing_table: str | None = None,
        schema: str | None = None,
        table: str | None = None,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        email: str | None = None,
        subject: str | None = None,
        queue: str | Sequence[str] | None = None,
        acknowledge_advisor: bool = False,
        wait_for_slot: float | None = None,
    ) -> Job:
        """Validate, admit, and hand off one Job; return it before it runs.

        Calling this method is the confirmation the CLI spells ``--yes``.
        Advisor error-severity findings still gate the launch: the first call
        raises :class:`UsageError` naming the rules, and passing
        ``acknowledge_advisor=True`` launches the SQL as written.

        Only two Jobs may be Pending or Running at once. Without
        ``wait_for_slot`` a third launch raises :class:`OperationalError` at
        once; with it, Dispatch retries until a slot frees or the given number
        of seconds passes.
        """
        argv = _launch_argv(
            source=source,
            destination=destination,
            sql=sql,
            existing_table=existing_table,
            schema=schema,
            table=table,
            start_date=start_date,
            end_date=end_date,
            email=email,
            subject=subject,
            queue=queue,
            acknowledge_advisor=acknowledge_advisor,
        )
        payload = self._launch_payload(argv, wait_for_slot=wait_for_slot)
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            raise OperationalError(f"Launch returned no Job ID: {payload!r}")
        return Job(self, {"id": job_id, "state": payload.get("state")}).refresh()

    def cleanup(self, *, older_than_days: float = DEFAULT_CLEANUP_DAYS) -> CleanupReport:
        """Delete Notebook workspace directories older than ``older_than_days``.

        Only the workspace is touched: Job manifests, run logs, and Results the
        Analyst asked for by name are left alone.
        """
        if older_than_days < 0:
            raise ValueError("older_than_days must be >= 0")
        if not self._workspace.is_dir():
            return CleanupReport(directories=0, bytes_freed=0)
        cutoff = time.time() - older_than_days * 86400
        directories = 0
        freed = 0
        for entry in sorted(self._workspace.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.stat().st_mtime > cutoff:
                continue
            freed += _directory_size(entry)
            _discard_dir(entry)
            directories += 1
        return CleanupReport(directories=directories, bytes_freed=freed)

    def jobs(self, state: JobState | None = None) -> JobList:
        """Return every Job, newest state first reconciled, optionally filtered."""
        args = ["list"] if state is None else ["list", "--state", state]
        payload = self._json(args)
        items = payload.get("jobs") or []
        return JobList(Job(self, item) for item in items)

    def job(self, job_id: str) -> Job:
        """Return one Job by ID, with its manifest loaded."""
        return Job(self, {"id": job_id}).refresh()

    def _launch_payload(
        self, argv: Sequence[str], *, wait_for_slot: float | None
    ) -> dict[str, Any]:
        """Launch, optionally retrying while the two-Job cap is full."""
        if wait_for_slot is None:
            return self._json(argv)
        if wait_for_slot < 0:
            raise ValueError("wait_for_slot must be >= 0")
        deadline = time.monotonic() + wait_for_slot
        while True:
            try:
                return self._json(argv)
            except OperationalError as exc:
                if not _is_capacity_refusal(exc):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(_SLOT_RETRY_INTERVAL, remaining))

    def _at(self, cwd: Path) -> Dispatch:
        """A session identical to this one but invoking the CLI in ``cwd``."""
        return Dispatch(
            cwd,
            command=self._command,
            env=self._env,
            timeout=self._timeout,
            workspace=self._workspace,
        )

    def _new_query_dir(self, stem: str) -> Path:
        """Create the per-query workspace directory holding SQL and its Result."""
        query_dir = self._workspace / f"{stem}_{secrets.token_hex(3)}"
        config.ensure_private_dir(self._workspace)
        query_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        return query_dir

    def _default_workspace(self) -> Path:
        root = self._env.get("DISPATCH_DATA_ROOT")
        if root:
            return config.notebook_dir(root=Path(root))
        return config.notebook_dir()

    def _json(self, args: Sequence[str]) -> dict[str, Any]:
        completed = self._run([*args, "--json"])
        try:
            payload = json.loads(completed.stdout)
        except ValueError as exc:
            raise OperationalError(
                f"Could not parse JSON from 'dispatch job {args[0]}': {completed.stdout!r}",
                argv=self._argv(args),
            ) from exc
        if not isinstance(payload, dict):
            raise OperationalError(
                f"Unexpected JSON payload from 'dispatch job {args[0]}': {payload!r}",
                argv=self._argv(args),
            )
        return payload

    def _text(self, args: Sequence[str]) -> str:
        return self._run(args).stdout

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = self._argv(args)
        try:
            completed = subprocess.run(
                argv,
                cwd=str(self._cwd),
                env=self._process_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise OperationalError(
                f"Could not run the Dispatch CLI ({shlex.join(argv)}): {exc}. "
                f"Set ${CLI_ENV_VAR} or pass command=[...] to point at the dispatch launcher.",
                argv=argv,
            ) from exc
        except NotADirectoryError as exc:
            raise OperationalError(
                f"Working directory is not usable: {self._cwd}", argv=argv
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise OperationalError(
                f"'dispatch job {args[0]}' exceeded the {self._timeout}s command timeout",
                argv=argv,
            ) from exc
        if completed.returncode != 0:
            raise _error_for(completed, argv)
        return completed

    def _popen(self, args: Sequence[str]) -> subprocess.Popen[str]:
        argv = self._argv(args)
        try:
            return subprocess.Popen(
                argv,
                cwd=str(self._cwd),
                env=self._process_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise OperationalError(
                f"Could not run the Dispatch CLI ({shlex.join(argv)}): {exc}",
                argv=argv,
            ) from exc

    def _argv(self, args: Sequence[str]) -> list[str]:
        return [*self._command, "job", *args]

    def _process_env(self) -> dict[str, str]:
        return {**os.environ, **self._env}

    def __repr__(self) -> str:
        return f"Dispatch(cwd={str(self._cwd)!r})"


class Job:
    """One Job, backed by the CLI's JSON view of its manifest.

    Attributes are read from the last CLI response; :meth:`refresh` re-reads
    the reconciled manifest.
    """

    def __init__(self, dispatch: Dispatch, data: Mapping[str, Any]) -> None:
        self._dispatch = dispatch
        self._data: dict[str, Any] = dict(data)

    @property
    def id(self) -> str:
        return str(self._data["id"])

    @property
    def state(self) -> str:
        return str(self._data.get("state") or "Pending")

    @property
    def source(self) -> str | None:
        return self._data.get("source")

    @property
    def destination(self) -> str | None:
        return self._data.get("destination")

    @property
    def source_detail(self) -> str | None:
        return self._data.get("source_detail")

    @property
    def destination_detail(self) -> str | None:
        return self._data.get("destination_detail")

    @property
    def created_at(self) -> str | None:
        return self._data.get("created_at")

    @property
    def started_at(self) -> str | None:
        return self._data.get("started_at")

    @property
    def finished_at(self) -> str | None:
        return self._data.get("finished_at")

    @property
    def pid(self) -> int | None:
        return self._data.get("pid")

    @property
    def exit_code(self) -> int | None:
        return self._data.get("exit_code")

    @property
    def user(self) -> str | None:
        return self._data.get("user")

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._data.get("params") or {})

    @property
    def manifest(self) -> dict[str, Any]:
        """The full manifest, refreshed on first access if only a summary is loaded."""
        if "manifest" not in self._data:
            self.refresh()
        return dict(self._data.get("manifest") or {})

    @property
    def csv_path(self) -> str | None:
        """Absolute path of the CSV result, or ``None`` for Table-only Jobs."""
        return (self.manifest.get("destination") or {}).get("csv_path") or None

    @property
    def result_path(self) -> Path | None:
        """Path of the Job's Result, or ``None`` when its Destination wrote none."""
        if (self.destination or "") not in ("Csv", "Table+Csv"):
            return None
        csv_path = self.csv_path
        return Path(csv_path) if csv_path else None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def succeeded(self) -> bool:
        return self.state == "Succeeded"

    @property
    def failed(self) -> bool:
        return self.state == "Failed"

    @property
    def cancelled(self) -> bool:
        return self.state == "Cancelled"

    @property
    def elapsed_seconds(self) -> float | None:
        """Runtime so far, or total runtime once the Job is terminal."""
        started = _parse_timestamp(self.started_at)
        if started is None:
            return None
        end = _parse_timestamp(self.finished_at) or datetime.now(timezone.utc)
        return max(0.0, (end - started).total_seconds())

    def refresh(self) -> Job:
        """Re-read the reconciled manifest from ``dispatch job show``."""
        self._data = self._dispatch._json(["show", self.id])
        return self

    def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_poll: Callable[[Job], None] | None = None,
    ) -> Job:
        """Poll until the Job is terminal, then return it.

        Failed and Cancelled Jobs are returned like Succeeded ones; inspect
        :attr:`succeeded`. Raises :class:`WaitTimeout` when ``timeout``
        elapses first. ``on_poll`` is called with this Job after every poll.
        """
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self.refresh()
            if on_poll is not None:
                on_poll(self)
            if self.is_terminal:
                return self
            if deadline is not None and time.monotonic() >= deadline:
                raise WaitTimeout(f"Job {self.id} is still {self.state} after {timeout}s")
            sleep_for = poll_interval
            if deadline is not None:
                sleep_for = min(poll_interval, max(0.0, deadline - time.monotonic()))
            time.sleep(sleep_for)

    def logs(self, *, lines: int = DEFAULT_LOG_LINES) -> str:
        """Return the last ``lines`` of the Job log."""
        return self._dispatch._text(["logs", self.id, "--lines", str(lines)])

    def stream_logs(self, *, lines: int = DEFAULT_LOG_LINES) -> Iterator[str]:
        """Yield log lines, following the log until the Job is terminal."""
        process = self._dispatch._popen(["logs", self.id, "--lines", str(lines), "--follow"])
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    yield line.rstrip("\n")
        finally:
            _shutdown(process)

    def print_logs(self, *, lines: int = DEFAULT_LOG_LINES, follow: bool = False) -> None:
        """Print the log tail, optionally following it until the Job is terminal."""
        if not follow:
            print(self.logs(lines=lines), end="")
            return
        for line in self.stream_logs(lines=lines):
            print(line, flush=True)

    def watch(
        self,
        *,
        lines: int = WATCH_LOG_LINES,
        timeout: float | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> Job:
        """Refresh state and the log tail in place until the Job is terminal.

        In Jupyter this updates one output cell; elsewhere it prints a line per
        state change. Interrupting the wait leaves the Job running: the runner
        is detached.
        """
        view = notebook_display.LiveView()
        return self.wait(
            timeout=timeout,
            poll_interval=poll_interval,
            on_poll=lambda job: view.update(job, job.logs(lines=lines).splitlines()),
        )

    def cancel(self) -> Job:
        """Cancel a Pending or Running Job; calling this is the confirmation."""
        self._dispatch._json(["cancel", self.id, "--yes"])
        return self.refresh()

    def to_df(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        **read_csv_kwargs: Any,
    ) -> Any:
        """Return the Job's Result as a ``pandas.DataFrame``, waiting if needed.

        Keyword arguments reach ``pandas.read_csv`` (``dtype``, ``parse_dates``,
        ``nrows``). The exported CSV is unquoted, so a value containing a comma
        or newline makes it ambiguous: prefer :meth:`rows` when you need the
        parse checked (ADR-0010).
        """
        return results.to_dataframe(
            self._result_path_when_successful(timeout=timeout, poll_interval=poll_interval),
            **read_csv_kwargs,
        )

    #: ``to_pandas`` reads the same way; both names exist because both are habits.
    to_pandas = to_df

    def rows(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> list[dict[str, str]]:
        """Return the Job's Result as a list of dicts, waiting if needed.

        Every line's field count is checked against the header, so an ambiguous
        export raises :class:`ResultParseError` instead of shifting columns.
        """
        return results.read_rows(
            self._result_path_when_successful(timeout=timeout, poll_interval=poll_interval)
        )

    @property
    def columns(self) -> list[str]:
        """Column names of the Job's Result."""
        return results.read_columns(self._result_path_when_successful())

    def to_csv(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> Path:
        """Copy the Job's Result to ``path`` and return where it landed."""
        source = self._result_path_when_successful(timeout=timeout, poll_interval=poll_interval)
        target = Path(path).expanduser()
        if target.is_dir():
            target = target / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return target

    def _result_path_when_successful(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> Path:
        """Wait for the Job, insist it Succeeded, and return its Result path."""
        if not self.is_terminal:
            self.wait(timeout=timeout, poll_interval=poll_interval)
        if not self.succeeded:
            raise JobUnsuccessful(
                f"Job {self.id} is {self.state} (exit code {self.exit_code}), so it has no "
                "Result. Read job.logs() to see why."
            )
        return results.resolve_result_path(self.result_path)

    def to_dict(self) -> dict[str, Any]:
        """The Job's fields as a plain dict, ready for pandas or JSON."""
        return dict(self._data)

    def __repr__(self) -> str:
        route = f"{self.source or '--'}->{self.destination or '--'}"
        return f"<Job {self.id} {self.state} {route}>"

    def _repr_html_(self) -> str:
        return notebook_display.job_html(self)


class JobList(list[Job]):
    """A list of :class:`Job` that renders as a table in Jupyter."""

    def to_dicts(self) -> list[dict[str, Any]]:
        """Rows for ``pandas.DataFrame(...)`` or JSON export."""
        return [job.to_dict() for job in self]

    def _repr_html_(self) -> str:
        return notebook_display.job_list_html(self)


class CleanupReport:
    """What :meth:`Dispatch.cleanup` removed from the Notebook workspace."""

    def __init__(self, *, directories: int, bytes_freed: int) -> None:
        self.directories = directories
        self.bytes_freed = bytes_freed

    def __repr__(self) -> str:
        return f"CleanupReport(directories={self.directories}, bytes_freed={self.bytes_freed})"


def _launch_argv(
    *,
    source: str,
    destination: str,
    sql: str | os.PathLike[str] | None,
    existing_table: str | None,
    schema: str | None,
    table: str | None,
    start_date: str | date | None,
    end_date: str | date | None,
    email: str | None,
    subject: str | None,
    queue: str | Sequence[str] | None,
    acknowledge_advisor: bool,
) -> list[str]:
    """Build ``launch`` arguments; omitted values keep the CLI's own defaults."""
    args = ["launch", "--source", source, "--destination", destination, "--yes"]
    optional = (
        ("--sql", "" if sql is None else os.fspath(sql)),
        ("--existing-table", existing_table),
        ("--schema", schema),
        ("--table", table),
        ("--start-date", _date_text(start_date)),
        ("--end-date", _date_text(end_date)),
        ("--email", email),
        ("--subject", subject),
        ("--queue", _queue_text(queue)),
    )
    for flag, value in optional:
        if value:
            args += [flag, str(value)]
    if acknowledge_advisor:
        args.append("--acknowledge-advisor")
    return args


def _query_stem() -> str:
    """A CSV-safe stem for one Inline SQL query (``safe_csv_path`` needs an identifier)."""
    return f"nb_{secrets.token_hex(4)}"


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else f"{text}\n"


def _is_capacity_refusal(error: DispatchError) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _CAPACITY_MARKERS)


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _discard_dir(path: Path) -> None:
    """Remove a workspace directory, tolerating a partially created one."""
    shutil.rmtree(path, ignore_errors=True)


def _date_text(value: str | date | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _queue_text(value: str | Sequence[str] | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return ",".join(str(pool) for pool in value)


def _error_for(completed: subprocess.CompletedProcess[str], argv: Sequence[str]) -> DispatchError:
    message = _error_message(completed.stderr) or (
        f"{shlex.join(list(argv))} exited with code {completed.returncode}"
    )
    error_class = _EXIT_ERRORS.get(completed.returncode, DispatchError)
    return error_class(
        message,
        argv=argv,
        exit_code=completed.returncode,
        stderr=completed.stderr,
    )


def _error_message(stderr: str) -> str:
    """Prefer the CLI's JSON ``error`` field, falling back to raw stderr."""
    text = stderr.strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return text


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _shutdown(process: subprocess.Popen[str]) -> None:
    """Stop a follow subprocess without leaking it when a generator is abandoned."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if process.stdout is not None:
        process.stdout.close()

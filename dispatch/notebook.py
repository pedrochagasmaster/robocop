"""Notebook API for launching and monitoring Jobs.

Thin adapter over the ``dispatch job`` CLI: every operation runs
``dispatch job … --json`` in a subprocess and parses one JSON document, so
notebooks inherit the CLI's validation, Kerberos checks, Advisor gates,
capacity admission, manifest persistence, and telemetry unchanged. Nothing in
this module reimplements Job behavior, and nothing here imports Textual.

    from dispatch.notebook import Dispatch

    d = Dispatch(cwd="~/sql")
    job = d.launch(source="SqlFile", destination="Csv", sql="query.sql", table="report")
    job.watch()          # live state and log tail until the Job is terminal
    job.succeeded        # True / False
    d.jobs()             # every Job, rendered as a table in Jupyter

Refused commands raise: :class:`UsageError` (invalid inputs, Advisor
acknowledgement), :class:`UnknownJobError`, :class:`OperationalError`
(Kerberos, capacity, handoff). A Job that ran and failed is data, not an
exception, so :meth:`Job.wait` returns the Job in every terminal state.
"""

from __future__ import annotations

import html
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .runtime import is_jupyter_notebook

try:  # Optional: self-updating rich output exists only inside IPython kernels.
    from IPython.display import HTML, display
except ImportError:
    HTML = None
    display = None

SourceType = Literal["SqlFile", "SqlTemplate", "ExistingTable"]
DestinationType = Literal["Table", "Csv", "Table+Csv"]
JobState = Literal["Pending", "Running", "Succeeded", "Failed", "Cancelled"]

TERMINAL_STATES = frozenset({"Succeeded", "Failed", "Cancelled"})

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_LOG_LINES = 50
WATCH_LOG_LINES = 12

#: Override the CLI invocation, e.g. ``DISPATCH_CLI=/ads_storage/dispatch/bin/dispatch``.
CLI_ENV_VAR = "DISPATCH_CLI"

_STATE_COLORS = {
    "Pending": "#8a6d00",
    "Running": "#0b5cad",
    "Succeeded": "#1a7f37",
    "Failed": "#b42318",
    "Cancelled": "#57606a",
}


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
    """

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        command: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        self._cwd = Path(cwd).expanduser() if cwd is not None else Path.cwd()
        self._command = list(command) if command is not None else cli_command()
        self._env = dict(env or {})
        self._timeout = timeout

    @property
    def cwd(self) -> Path:
        """Directory the CLI runs in; CSV results land here."""
        return self._cwd

    @property
    def command(self) -> list[str]:
        """CLI invocation this session shells out to."""
        return list(self._command)

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
    ) -> Job:
        """Validate, admit, and hand off one Job; return it before it runs.

        Calling this method is the confirmation the CLI spells ``--yes``.
        Advisor error-severity findings still gate the launch: the first call
        raises :class:`UsageError` naming the rules, and passing
        ``acknowledge_advisor=True`` launches the SQL as written.
        """
        payload = self._json(
            _launch_argv(
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
        )
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            raise OperationalError(f"Launch returned no Job ID: {payload!r}")
        return Job(self, {"id": job_id, "state": payload.get("state")}).refresh()

    def jobs(self, state: JobState | None = None) -> JobList:
        """Return every Job, newest state first reconciled, optionally filtered."""
        args = ["list"] if state is None else ["list", "--state", state]
        payload = self._json(args)
        items = payload.get("jobs") or []
        return JobList(Job(self, item) for item in items)

    def job(self, job_id: str) -> Job:
        """Return one Job by ID, with its manifest loaded."""
        return Job(self, {"id": job_id}).refresh()

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
        view = _LiveView()
        return self.wait(
            timeout=timeout,
            poll_interval=poll_interval,
            on_poll=lambda job: view.update(job, job.logs(lines=lines).splitlines()),
        )

    def cancel(self) -> Job:
        """Cancel a Pending or Running Job; calling this is the confirmation."""
        self._dispatch._json(["cancel", self.id, "--yes"])
        return self.refresh()

    def to_dict(self) -> dict[str, Any]:
        """The Job's fields as a plain dict, ready for pandas or JSON."""
        return dict(self._data)

    def __repr__(self) -> str:
        route = f"{self.source or '--'}->{self.destination or '--'}"
        return f"<Job {self.id} {self.state} {route}>"

    def _repr_html_(self) -> str:
        rows = [
            ("Job ID", self.id),
            ("State", _state_html(self.state)),
            ("Source", f"{self.source or '--'} {self.source_detail or ''}".strip()),
            ("Destination", f"{self.destination or '--'} {self.destination_detail or ''}".strip()),
            ("Elapsed", _format_duration(self.elapsed_seconds)),
            ("Exit code", "--" if self.exit_code is None else str(self.exit_code)),
        ]
        cells = "".join(
            f"<tr><th style='text-align:left;padding:2px 12px 2px 0'>{html.escape(label)}</th>"
            f"<td style='text-align:left'>{value}</td></tr>"
            for label, value in rows
        )
        return f"<table style='border:none'><tbody>{cells}</tbody></table>"


class JobList(list[Job]):
    """A list of :class:`Job` that renders as a table in Jupyter."""

    def to_dicts(self) -> list[dict[str, Any]]:
        """Rows for ``pandas.DataFrame(...)`` or JSON export."""
        return [job.to_dict() for job in self]

    def _repr_html_(self) -> str:
        if not self:
            return "<em>No Jobs.</em>"
        headers = ("Job ID", "State", "Source", "Destination", "Elapsed", "Exit")
        head = "".join(
            f"<th style='text-align:left;padding-right:12px'>{name}</th>" for name in headers
        )
        body = []
        for job in self:
            cells = (
                html.escape(job.id),
                _state_html(job.state),
                html.escape(job.source or "--"),
                html.escape(job.destination or "--"),
                _format_duration(job.elapsed_seconds),
                "--" if job.exit_code is None else str(job.exit_code),
            )
            body.append(
                "<tr>"
                + "".join(f"<td style='padding-right:12px'>{cell}</td>" for cell in cells)
                + "</tr>"
            )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


class _LiveView:
    """Render repeated Job snapshots in place (Jupyter) or as change lines (stdout)."""

    def __init__(self, *, rich: bool | None = None) -> None:
        available = display is not None and HTML is not None
        self._rich = (is_jupyter_notebook() and available) if rich is None else (rich and available)
        self._handle: Any = None
        self._last = ""

    def update(self, job: Job, log_lines: Sequence[str]) -> None:
        if self._rich:
            payload = HTML(_watch_html(job, log_lines))
            if self._handle is None:
                self._handle = display(payload, display_id=True)
            else:
                self._handle.update(payload)
            return
        summary = f"{job.id} {job.state} ({_format_duration(job.elapsed_seconds)})"
        if summary != self._last:
            print(summary, flush=True)
            self._last = summary


def _watch_html(job: Job, log_lines: Sequence[str]) -> str:
    log = html.escape("\n".join(log_lines)) or "(no log output yet)"
    return (
        "<div style='font-family:monospace'>"
        f"<div><b>{html.escape(job.id)}</b> {_state_html(job.state)} "
        f"&middot; {html.escape(job.destination or '--')} "
        f"&middot; {_format_duration(job.elapsed_seconds)}</div>"
        "<pre style='margin:4px 0 0;padding:8px;background:#f6f8fa;"
        f"max-height:18em;overflow:auto'>{log}</pre>"
        "</div>"
    )


def _state_html(state: str) -> str:
    color = _STATE_COLORS.get(state, "#57606a")
    return f"<span style='color:{color};font-weight:600'>{html.escape(state)}</span>"


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


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


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

# pylint: disable=logging-fstring-interpolation,too-many-return-statements,too-many-branches
import json
import logging
import os
import re
import smtplib
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Callable
from urllib.parse import parse_qs, urlsplit


FATAL_ERRORS = {"TABLE_NOT_FOUND", "SYNTAX_ERROR", "DUPLICATE_COLUMN", "AUTH_ERROR", "GENERIC_ERROR"}
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FULL_TABLE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*"
)
SMTP_TIMEOUT_SECONDS = 30.0

# ---------------------------------------------------------------------------
# Execution identity event protocol (docs/research/impala-debug-web-
# monitoring-contract.md "Dispatch-specific execution seam").
#
# ``run_impala_shell`` is a narrow, defensive replacement for the
# ``Popen(...).communicate()`` pairs in the two Impala launch call sites. It
# preserves the exact bytes and exit code those call sites already depend on
# for ``classificar_erro_impala`` and CSV/email handling, while opportunistically
# capturing the impala-shell "monitor URL" seam as bounded JSON Lines events
# for Dispatch to consume out-of-band. No part of this protocol can affect
# the child's exit code or returned bytes: every monitoring action is wrapped
# so a bad path, full disk, or malformed line degrades to a no-op.
# ---------------------------------------------------------------------------

# Version-stamped so a future gate-2 capture of the production shell's exact
# wording can adjust these in one place. Anchored to the fixed prefixes
# documented in the research note; everything else on stderr passes through
# uninterpreted.
MONITOR_LINE_RE_V1 = re.compile(r"Query state can be monitored at:\s*(\S+)")
RETRIED_LINE_RE_V1 = re.compile(r"Retried query link:\s*(\S+)")

MONITOR_EVENTS_MAX_BYTES = 1 * 1024 * 1024  # ~1 MiB cap on the sidecar file.

_QUERY_ID_RE = re.compile(r"\A[0-9a-f]{16}:[0-9a-f]{16}\Z")


def _validate_coordinator_url(url: str) -> str | None:
    """Minimal local validation of a coordinator base URL.

    ``scr/`` cannot import ``dispatch`` (see ADR-0005), so this duplicates
    only the narrow slice of ``dispatch.impala_monitor.validate_coordinator_url``
    needed here: scheme must be ``http``/``https``, host must be present, no
    userinfo. Returns the normalized ``scheme://host[:port]`` base URL, or
    ``None`` when the URL does not pass validation.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = parts.hostname if port is None else f"{parts.hostname}:{port}"
    return f"{parts.scheme}://{netloc}"


def _extract_query_id(url: str) -> str | None:
    """Pull ``query_id=<id>`` from a monitor/retried-link URL and validate it."""
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    values = query.get("query_id")
    if not values:
        return None
    candidate = values[0]
    if not _QUERY_ID_RE.match(candidate):
        return None
    return candidate


class _EventWriter:
    """Append bounded JSON Lines monitoring events to the sidecar file.

    Every public method swallows all exceptions: nothing here may ever raise
    into the caller, change the child's exit code, or alter the bytes
    ``run_impala_shell`` returns. When ``events_path`` is falsy the writer is
    a no-op by construction (never opens a file).
    """

    def __init__(self, events_path: str | None, job_id: str, pool: str) -> None:
        self._path = events_path or None
        self._job_id = job_id
        self._pool = pool
        self._shell_execution_id = uuid.uuid4().hex
        self._seq = 0
        self._closed = False
        self._lock = threading.Lock()

    def _write(self, event_type: str, extra: dict) -> None:
        if not self._path or self._closed:
            return
        try:
            with self._lock:
                self._seq += 1
                payload = {
                    "v": 1,
                    "type": event_type,
                    "job_id": self._job_id,
                    "shell_execution_id": self._shell_execution_id,
                    "seq": self._seq,
                    "pool": self._pool,
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    **extra,
                }
                line = json.dumps(payload, sort_keys=True) + "\n"
                try:
                    existing_size = os.path.getsize(self._path)
                except OSError:
                    existing_size = 0
                if existing_size >= MONITOR_EVENTS_MAX_BYTES:
                    self._closed = True
                    return
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception:  # pylint: disable=broad-except
            # Monitoring must never raise into the caller; degrade silently.
            self._closed = True

    def shell_started(self) -> None:
        self._write("shell_started", {})

    def query_discovered(self, url: str) -> None:
        base = _validate_coordinator_url(url)
        qid = _extract_query_id(url)
        if base is None or qid is None:
            return
        self._write(
            "query_discovered",
            {"coordinator_base_url": base, "query_id": qid},
        )

    def query_retried(self, url: str) -> None:
        base = _validate_coordinator_url(url)
        qid = _extract_query_id(url)
        if base is None or qid is None:
            return
        self._write(
            "query_retried",
            {"coordinator_base_url": base, "query_id": qid},
        )

    def shell_finished(self, returncode: int | None) -> None:
        self._write("shell_finished", {"returncode": returncode})


def _drain_stdout(pipe, chunks: list) -> None:
    try:
        chunks.append(pipe.read())
    except Exception:  # pylint: disable=broad-except
        chunks.append(b"")


def _drain_stderr(pipe, chunks: list, writer: "_EventWriter") -> None:
    """Read stderr line-by-line, forwarding every byte while scanning for
    the two anchored monitor-line shapes. Scanning failures never drop or
    alter output bytes: the accumulated chunks are exactly what was read.
    """
    try:
        for raw_line in pipe:
            chunks.append(raw_line)
            try:
                text = raw_line.decode("utf-8", errors="replace")
                monitor_match = MONITOR_LINE_RE_V1.search(text)
                if monitor_match:
                    writer.query_discovered(monitor_match.group(1))
                retried_match = RETRIED_LINE_RE_V1.search(text)
                if retried_match:
                    writer.query_retried(retried_match.group(1))
            except Exception:  # pylint: disable=broad-except
                pass
    except Exception:  # pylint: disable=broad-except
        pass


def run_impala_shell(argv: list[str], *, pool: str = "") -> tuple[int, bytes, bytes]:
    """Spawn ``impala-shell`` and drain stdout/stderr concurrently.

    Two daemon reader threads drain stdout and stderr at the same time,
    which avoids the classic pipe deadlock (a child blocked writing to a
    full stdout pipe while the parent is only reading stderr, or vice
    versa). The returned ``(returncode, stdout_bytes, stderr_bytes)`` tuple
    is exactly what ``Popen(...).communicate()`` returns today, so
    downstream error classification (``classificar_erro_impala``) sees
    identical input.

    While draining stderr, this recognizes only two anchored line shapes
    from the shell source (see ``MONITOR_LINE_RE_V1`` / ``RETRIED_LINE_RE_V1``)
    and, when ``DISPATCH_MONITOR_EVENTS_PATH`` is set in the environment,
    appends bounded JSON Lines events (``shell_started``, ``query_discovered``,
    ``query_retried``, ``shell_finished``) to that path. No SQL and no error
    bodies are ever included in an event. When the env var is unset, this
    degrades to a plain concurrent drain with no events emitted.

    Every monitoring action is wrapped so no exception, full disk, or bad
    path can change the child's exit code, the returned bytes, or the
    process timing beyond the drain itself.
    """
    events_path = os.environ.get("DISPATCH_MONITOR_EVENTS_PATH", "")
    job_id = os.environ.get("DISPATCH_JOB_ID", "")
    writer = _EventWriter(events_path, job_id, pool)

    try:
        writer.shell_started()
    except Exception:  # pylint: disable=broad-except
        pass

    process = subprocess.Popen(  # pylint: disable=consider-using-with
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    stdout_thread = threading.Thread(
        target=_drain_stdout, args=(process.stdout, stdout_chunks), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain_stderr, args=(process.stderr, stderr_chunks, writer), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()

    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()

    try:
        writer.shell_finished(returncode)
    except Exception:  # pylint: disable=broad-except
        pass

    return returncode, b"".join(stdout_chunks), b"".join(stderr_chunks)


def validate_identifier(value: str) -> bool:
    return IDENTIFIER_RE.fullmatch(value) is not None


def validate_full_table(value: str) -> bool:
    return FULL_TABLE_RE.fullmatch(value) is not None


def send_email(messageBody, subject, to_email):
    msg = MIMEMultipart()
    from_email = 'AutoQueryExecution_Analytics@mastercard.com'
    msg['From'] = from_email
    msg['TO'] = to_email
    msg['Date'] = formatdate(localtime=True)
    msg['Subject'] = subject
    msg.attach(MIMEText(messageBody))

    try:
        mailhost = os.environ.get("MAILHOST", "mailhost.mclocal.int")
        host, _, port = mailhost.partition(":")
        server = smtplib.SMTP(host, int(port) if port else 0, timeout=SMTP_TIMEOUT_SECONDS)
        try:
            server.sendmail(from_email, to_email.split(';'), msg.as_string())
        finally:
            server.quit()
        logging.info(f"Email sent to {to_email} with subject: {subject}")
    except Exception as e:
        logging.error(f"Failed to send email. Error: {e}")


def classificar_erro_impala(stderr_text: str) -> dict:
    texto = stderr_text.lower()
    details = {"detalhes": stderr_text}
    if "memory limit exceeded" in texto:
        return {"categoria": "MEMORY_EXCEEDED", **details}
    if "not enough memory available" in texto:
        return {"categoria": "MEMORY_EXCEEDED", **details}
    if "syntax error" in texto or "parseexception" in texto:
        return {"categoria": "SYNTAX_ERROR", **details}
    if "authenticationexception" in texto:
        return {"categoria": "AUTH_ERROR", **details}
    if "table not found" in texto or "could not resolve" in texto:
        return {"categoria": "TABLE_NOT_FOUND", **details}
    if "timed out" in texto or "deadline exceeded" in texto:
        return {"categoria": "TIMEOUT", **details}
    if "queue is full" in texto or "no resources available" in texto:
        return {"categoria": "QUEUE_FULL", **details}
    if "could not connect" in texto or "connection refused" in texto:
        return {"categoria": "CONNECTION_ERROR", **details}
    if "dropped due to backpressure" in texto:
        return {"categoria": "BACKPRESSURE", **details}
    if "could not resolve host" in texto or "name or service not known" in texto:
        return {"categoria": "HOST_RESOLUTION_ERROR", **details}
    if "invalid credentials" in texto or "authentication failed" in texto:
        return {"categoria": "AUTH_ERROR", **details}
    if "invalid or unknown query handle" in texto:
        return {"categoria": "TIMEOUT", **details}
    if "time limit" in texto:
        return {"categoria": "TIMEOUT", **details}
    if "duplicate column name" in texto:
        return {"categoria": "DUPLICATE_COLUMN", **details}
    if "unreachable" in texto:
        return {"categoria": "HOST_UNREACHABLE", **details}
    if "diskspace" in texto or "disk full" in texto:
        return {"categoria": "DISK_FULL", **details}
    if "memory available" in texto:
        return {"categoria": "MEMORY_AVAILABLE", **details}
    if "space limit" in texto:
        return {"categoria": "SPACE_LIMIT", **details}
    if "timeout" in texto:
        return {"categoria": "TIMEOUT", **details}
    return {"categoria": "GENERIC_ERROR", **details}


def resolve_pools(default_pools: list[str]) -> list[str]:
    """Return the Impala request-pool (queue) list to cycle through.

    Reads the ``DISPATCH_REQUEST_POOL`` environment variable, a comma-separated
    list of request pools. When it is set to a non-empty value, those pools
    replace the caller's default for this single run; when it is unset or empty
    the caller's hardcoded default list is returned unchanged, preserving the
    historical cycling behaviour exactly.

    This is the ADR-0005 "configuration externalised via env vars with the
    current hardcoded value as the default" pattern: the frozen queue-list
    values live in the callers and are never modified here, but the TUI can pin
    a job to a chosen queue by exporting ``DISPATCH_REQUEST_POOL`` before the
    orchestrator runs.
    """
    raw = os.environ.get("DISPATCH_REQUEST_POOL", "")
    selected = [pool.strip() for pool in raw.split(",") if pool.strip()]
    return selected or list(default_pools)


def cycle_through_pools(
    pools: list[str],
    operation: Callable[[str], bool],
    on_cycle_failure: Callable[[int], None],
    retry_interval: int = 30,
    max_cycles: int | None = None,
) -> bool:
    retry_cnt = 1
    while True:
        for pool in pools:
            if operation(pool):
                return True
        if max_cycles is not None and retry_cnt >= max_cycles:
            raise TimeoutError("Retry cycle limit reached.")
        on_cycle_failure(retry_cnt)
        retry_cnt += 1
        time.sleep(retry_interval)

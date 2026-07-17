"""Mock-friendly Impala metadata helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from . import process, sql
from .formatting import format_data_size, parse_data_size

QUERY_TIMEOUT_SECONDS = 30
MAX_QUERIES_PER_USER = 2
# Running Jobs finish outside this process, so occupy() rechecks periodically.
_EXTERNAL_SLOT_POLL_SECONDS = 0.25

IMPALA_BASE_ARGV = (
    "impala-shell",
    "-k",
    "-i",
    "dw.prod.impala.mastercard.int:21000",
    "--ssl",
    "--delimited",
    "--print_header",
    "--output_delimiter=|",
)


class QueryLedger:
    """Track in-process Impala queries against the per-user coordinator cap.

    Impala admits at most ``MAX_QUERIES_PER_USER`` concurrent queries per user.
    This ledger counts TUI-side ``impala-shell`` calls (SHOW/DESCRIBE/DROP/STATS).
    Running Jobs are counted separately via ``external_running_query_count`` —
    their orchestrators hold coordinator capacity outside this process.

    Invariant: ``in_flight + external_running_query_count()`` never exceeds
    ``MAX_QUERIES_PER_USER`` at the moment a slot is granted. Cancellation of an
    occupied call always releases the slot in ``finally``.
    """

    def __init__(self) -> None:
        self._in_flight = 0
        self._peak_total = 0
        self._changed = asyncio.Condition()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def peak_total(self) -> int:
        """Highest ``in_flight + external`` observed when a slot was granted."""
        return self._peak_total

    def _record_grant_locked(self) -> None:
        total = self._in_flight + external_running_query_count()
        if total > self._peak_total:
            self._peak_total = total

    @asynccontextmanager
    async def occupy(self) -> AsyncIterator[None]:
        """Block until a coordinator slot is free, then hold it for the call.

        Counts both in-process queries and Running Jobs. Polls while waiting so
        a Job finishing (which cannot notify this Condition) still unblocks.
        """
        async with self._changed:
            while self._in_flight + external_running_query_count() >= MAX_QUERIES_PER_USER:
                try:
                    await asyncio.wait_for(
                        self._changed.wait(), timeout=_EXTERNAL_SLOT_POLL_SECONDS
                    )
                except asyncio.TimeoutError:
                    continue
            self._in_flight += 1
            self._record_grant_locked()
        try:
            yield
        finally:
            async with self._changed:
                self._in_flight -= 1
                self._changed.notify_all()

    @asynccontextmanager
    async def try_occupy(self) -> AsyncIterator[bool]:
        """Non-blocking slot take for size fetch: False if the cap is already full."""
        async with self._changed:
            if self._in_flight + external_running_query_count() >= MAX_QUERIES_PER_USER:
                acquired = False
            else:
                self._in_flight += 1
                self._record_grant_locked()
                acquired = True
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            async with self._changed:
                self._in_flight -= 1
                self._changed.notify_all()


query_ledger = QueryLedger()


def reset_query_ledger_for_tests() -> None:
    """Reset the process-wide ledger between unit tests."""
    global query_ledger
    query_ledger = QueryLedger()


def external_running_query_count() -> int:
    """Count Running Jobs whose orchestrators may occupy Impala capacity.

    Wire-up note: this is the existing job-tracking seam. It only sees Jobs the
    TUI launched (manifest state ``Running``). It does **not** see:
    - ad-hoc ``impala-shell`` sessions outside Dispatch
    - queries from other Dispatch TUI processes for the same Kerberos user
    - brief gaps inside an orchestrator between Impala statements

    Those gaps mean this is a conservative proxy: a Running Job reserves a slot
    even if Impala is idle between statements. That keeps Browse size-fetch from
    stacking on top of live Jobs.
    """
    from . import jobs

    return len(jobs.running_jobs())


def total_running_queries() -> int:
    """TUI in-flight metadata queries plus Running Job orchestrators."""
    return query_ledger.in_flight + external_running_query_count()


def size_fetch_concurrency() -> int:
    """How many size queries may start now: ``max(0, 2 - current_running)``."""
    return max(0, MAX_QUERIES_PER_USER - total_running_queries())


async def _run_impala_shell(sql: str) -> str:
    try:
        rc, stdout, stderr = await process.run_exec(
            *IMPALA_BASE_ARGV, "-q", sql, timeout=QUERY_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        # str(TimeoutError()) is empty, which would surface as a blank error in
        # the Browser; give the user an actionable message instead.
        raise RuntimeError(f"impala-shell timed out after {QUERY_TIMEOUT_SECONDS}s") from None
    if rc != 0:
        raise RuntimeError(stderr or stdout or f"impala-shell exited {rc}")
    return stdout


async def query(sql: str) -> str:
    """Run an Impala statement, occupying one per-user coordinator slot."""
    async with query_ledger.occupy():
        return await _run_impala_shell(sql)


async def show_tables(schema: str, pattern: str = "*") -> list[str]:
    schema_error = sql.validate_identifier(schema, "Schema")
    if schema_error:
        raise ValueError(schema_error)
    if "'" in pattern:
        raise ValueError("SHOW TABLES pattern must not contain a single quote")
    output = await query(f"SHOW TABLES IN {schema} LIKE '{pattern}';")
    tables: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Mock "):
            continue
        # impala-shell runs with --print_header, so SHOW TABLES emits its single
        # "name" column header as the first row. It is not a table: keeping it
        # added a phantom entry, inflated the table count, and made the
        # auto-describe of row 0 fail with "Could not resolve path".
        if line == "name":
            continue
        tables.append(line)
    return tables


@dataclass(frozen=True)
class TableStats:
    size_bytes: int | None
    size_display: str


def parse_table_stats_output(raw: str) -> TableStats:
    """Parse pipe-delimited ``SHOW TABLE STATS`` output into total on-disk size."""
    size_index: int | None = None
    total_bytes = 0
    saw_size = False

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Mock "):
            continue
        parts = [part.strip() for part in line.split("|")]
        if not parts:
            continue
        if parts[0] == "#Rows":
            try:
                size_index = parts.index("Size")
            except ValueError:
                size_index = 2 if len(parts) > 2 else None
            continue
        if size_index is None or size_index >= len(parts):
            continue
        parsed = parse_data_size(parts[size_index])
        if parsed is None:
            continue
        total_bytes += parsed
        saw_size = True

    if not saw_size:
        return TableStats(size_bytes=None, size_display="—")
    return TableStats(size_bytes=total_bytes, size_display=format_data_size(total_bytes))


async def table_stats(full_table: str) -> TableStats:
    _require_full_table(full_table)
    output = await query(f"SHOW TABLE STATS {full_table};")
    return parse_table_stats_output(output)


# Preserved so ``iter_table_sizes`` can detect test monkeypatches of ``table_stats``.
_PRODUCTION_TABLE_STATS: Callable[[str], Coroutine[Any, Any, TableStats]] = table_stats


async def _table_stats_for_size_fetch(full_table: str) -> TableStats | None:
    """Fetch stats only if a free slot is available right now; else return None.

    Uses ``try_occupy`` so size fetch never queues behind a full coordinator and
    never stacks past the per-user cap of two (including Running Jobs).
    """
    _require_full_table(full_table)
    async with query_ledger.try_occupy() as acquired:
        if not acquired:
            return None
        # Bypass ``query()`` to avoid double-occupying the ledger.
        output = await _run_impala_shell(f"SHOW TABLE STATS {full_table};")
        return parse_table_stats_output(output)


async def iter_table_sizes(
    schema: str, table_names: list[str]
) -> AsyncIterator[tuple[str, TableStats]]:
    """Yield ``(table_name, stats)`` with concurrency ``max(0, 2 - running)``.

    The Impala coordinators cap parallel queries per user at two. Size fetch
    fills only the free slots after counting:

    - in-process metadata queries (``query_ledger``, via ``query()`` / try_occupy)
    - Running Jobs (``external_running_query_count`` / ``jobs.running_jobs``)

    When both slots are already taken, remaining tables yield unknown stats
    immediately rather than queueing behind interactive work. A table whose
    stats query fails also yields unknown stats without aborting the rest.
    """
    remaining = list(table_names)
    unknown = TableStats(size_bytes=None, size_display="—")

    while remaining:
        concurrency = size_fetch_concurrency()
        if concurrency <= 0:
            for table_name in remaining:
                yield table_name, unknown
            return

        batch = remaining[:concurrency]
        remaining = remaining[concurrency:]

        async def _one(table_name: str) -> tuple[str, TableStats]:
            full_table = table_name if "." in table_name else f"{schema}.{table_name}"
            try:
                if table_stats is _PRODUCTION_TABLE_STATS:
                    stats = await _table_stats_for_size_fetch(full_table)
                else:
                    # Test monkeypatch: call the replacement directly.
                    stats = await table_stats(full_table)
            except Exception:
                stats = unknown
            if stats is None:
                stats = unknown
            return table_name, stats

        results = await asyncio.gather(*(_one(name) for name in batch))
        for item in results:
            yield item


def _require_full_table(full_table: str) -> None:
    full_table_error = sql.validate_full_table(full_table, "Table")
    if full_table_error:
        raise ValueError(full_table_error)


async def describe_table(full_table: str) -> str:
    _require_full_table(full_table)
    return await query(f"DESCRIBE {full_table};")


async def drop_table(full_table: str) -> str:
    _require_full_table(full_table)
    return await query(f"DROP TABLE IF EXISTS {full_table};")

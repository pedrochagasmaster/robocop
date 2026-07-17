"""Mock-friendly Impala metadata helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from . import capacity, process, sql
from .asyncio_utils import await_uncancellable
from .formatting import format_data_size, parse_data_size

QUERY_TIMEOUT_SECONDS = 30
_SIZE_FETCH_BATCH_SIZE = 2

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


class ImpalaExecutionError(RuntimeError):
    """An expected impala-shell timeout or non-zero query result."""


async def _run_impala_shell(sql: str) -> str:
    try:
        rc, stdout, stderr = await process.run_exec(
            *IMPALA_BASE_ARGV, "-q", sql, timeout=QUERY_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        # str(TimeoutError()) is empty, which would surface as a blank error in
        # the Browser; give the user an actionable message instead.
        raise ImpalaExecutionError(
            f"impala-shell timed out after {QUERY_TIMEOUT_SECONDS}s"
        ) from None
    if rc != 0:
        raise ImpalaExecutionError(stderr or stdout or f"impala-shell exited {rc}")
    return stdout


async def _release_metadata(lease: capacity.MetadataLease) -> None:
    release = asyncio.create_task(asyncio.to_thread(lease.release))
    try:
        await asyncio.shield(release)
    except asyncio.CancelledError:
        await await_uncancellable(release)
        raise


async def _acquire_metadata(operation: str) -> capacity.MetadataLease:
    acquisition = asyncio.create_task(asyncio.to_thread(capacity.try_acquire_metadata, operation))
    try:
        return await asyncio.shield(acquisition)
    except asyncio.CancelledError as cancelled:
        try:
            lease = await await_uncancellable(acquisition)
        except Exception:
            raise cancelled
        await _release_metadata(lease)
        raise cancelled


async def query(sql: str) -> str:
    """Run one Impala metadata statement under a shared fail-fast lease."""
    lease = await _acquire_metadata(_operation_name(sql))
    try:
        return await _run_impala_shell(sql)
    finally:
        await _release_metadata(lease)


def _operation_name(statement: str) -> str:
    normalized = " ".join(statement.split()).upper()
    if normalized.startswith("SHOW TABLE STATS"):
        return "SHOW TABLE STATS"
    if normalized.startswith("SHOW TABLES"):
        return "SHOW TABLES"
    if normalized.startswith("DESCRIBE"):
        return "DESCRIBE"
    if normalized.startswith("DROP"):
        return "DROP"
    return "Impala metadata query"


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


async def iter_table_sizes(
    schema: str, table_names: list[str]
) -> AsyncIterator[tuple[str, TableStats]]:
    """Yield sizes in two-wide batches without hiding ledger failures."""
    remaining = list(table_names)
    unknown = TableStats(size_bytes=None, size_display="—")

    while remaining:
        batch = remaining[:_SIZE_FETCH_BATCH_SIZE]
        remaining = remaining[_SIZE_FETCH_BATCH_SIZE:]

        async def _one(table_name: str) -> tuple[str, TableStats]:
            full_table = table_name if "." in table_name else f"{schema}.{table_name}"
            try:
                stats = await table_stats(full_table)
            except (capacity.CapacityBusy, ImpalaExecutionError):
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

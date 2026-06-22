"""Mock-friendly Impala metadata helpers."""

from __future__ import annotations

import asyncio

from . import process

QUERY_TIMEOUT_SECONDS = 30

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


async def query(sql: str) -> str:
    try:
        rc, stdout, stderr = await process.run_exec(
            *IMPALA_BASE_ARGV, "-q", sql, timeout=QUERY_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        # str(TimeoutError()) is empty, which would surface as a blank error in
        # the Browser; give the user an actionable message instead.
        raise RuntimeError(
            f"impala-shell timed out after {QUERY_TIMEOUT_SECONDS}s"
        ) from None
    if rc != 0:
        raise RuntimeError(stderr or stdout or f"impala-shell exited {rc}")
    return stdout


async def show_tables(schema: str, pattern: str = "*") -> list[str]:
    output = await query(f"SHOW TABLES IN {schema} LIKE '{pattern}';")
    return [line.strip() for line in output.splitlines() if line.strip() and not line.startswith("Mock ")]


async def describe_table(full_table: str) -> str:
    return await query(f"DESCRIBE {full_table};")


async def drop_table(full_table: str) -> str:
    return await query(f"DROP TABLE IF EXISTS {full_table};")

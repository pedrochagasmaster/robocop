"""SQL detection and preview helpers."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from pathlib import Path

# Classic Impala identifiers for New Job, manifest, CSV, and other shared paths:
# letter or underscore first (unquoted SQL tokens). Digit-leading names are not
# accepted here — Browse catalog metadata uses CATALOG_* helpers instead.
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FULL_TABLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*")
# Catalog identifiers for Impala Browse metadata (SHOW/DESCRIBE/DROP/STATS).
# Digit-leading names are legal in the catalog but must be backtick-quoted in
# SQL; separators/comments/whitespace/quotes remain rejected.
CATALOG_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_]+")
CATALOG_FULL_TABLE_RE = re.compile(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]+")
DATE_INICIO_TOKEN = "{date_inicio}"
DATE_FIM_TOKEN = "{date_fim}"


def validate_identifier(value: str, label: str) -> str | None:
    """Return a clear error when ``value`` is not a plain Impala identifier."""
    if not IDENTIFIER_RE.fullmatch(value):
        return f"{label} must be a plain Impala identifier"
    return None


def validate_catalog_identifier(value: str, label: str) -> str | None:
    """Return an error unless ``value`` is a catalog-safe Impala identifier.

    Unlike :func:`validate_identifier`, digit-leading names are allowed so Browse
    can address catalog tables that are not valid unquoted SQL tokens.
    """
    if not CATALOG_IDENTIFIER_RE.fullmatch(value):
        return f"{label} must be a plain Impala identifier"
    return None


def validate_catalog_full_table(value: str, label: str = "table") -> str | None:
    """Return an error unless ``value`` is catalog-safe ``schema.table``."""
    if not CATALOG_FULL_TABLE_RE.fullmatch(value):
        return f"{label} must be schema.table using plain Impala identifiers"
    return None


def quote_identifier(value: str) -> str:
    """Render a catalog-validated identifier for Impala SQL.

    Classic names stay bare; digit-leading catalog names are wrapped in
    backticks. Callers must validate with catalog helpers first — this helper
    does not accept unchecked input.
    """
    if IDENTIFIER_RE.fullmatch(value):
        return value
    return f"`{value}`"


def format_full_table_sql(value: str, label: str = "table") -> str:
    """Validate catalog ``schema.table`` and return it rendered for Impala SQL."""
    error = validate_catalog_full_table(value, label)
    if error:
        raise ValueError(error)
    schema, table = value.split(".", 1)
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"


def eid_table_prefix(eid: str) -> str:
    """Return the fixed ``EID_`` prefix for user-owned table names."""
    return f"{eid}_"


def split_eid_table_suffix(full_or_suffix: str, eid: str) -> str:
    """Return the editable suffix from a full ``EID_suffix`` or bare suffix."""
    prefix = eid_table_prefix(eid)
    if full_or_suffix.startswith(prefix):
        return full_or_suffix[len(prefix) :]
    return full_or_suffix


def join_eid_table_name(eid: str, suffix: str) -> str:
    """Build the full table name ``EID_suffix``."""
    return f"{eid_table_prefix(eid)}{suffix.strip()}"


def validate_eid_table_name(full: str, eid: str, label: str = "Table name") -> str | None:
    """Return an error unless ``full`` is ``EID_suffix`` for the logged-in user."""
    prefix = eid_table_prefix(eid)
    if not full.startswith(prefix):
        return f"{label} must start with {prefix}"
    suffix = full[len(prefix) :]
    if not suffix:
        return f"{label} requires a suffix after {prefix}"
    suffix_error = validate_identifier(suffix, f"{label} suffix")
    if suffix_error:
        return suffix_error
    return validate_identifier(full, label)


def validate_full_table(value: str, label: str = "table") -> str | None:
    """Return a clear error unless ``value`` is exactly ``schema.table``."""
    if not FULL_TABLE_RE.fullmatch(value):
        return f"{label} must be schema.table using plain Impala identifiers"
    return None


def resolve_csv_output_path(launch_cwd: Path, raw_path: str | Path) -> Path:
    """Resolve a direct CSV output path inside ``launch_cwd``."""
    resolved_cwd = launch_cwd.resolve()
    candidate = Path(raw_path)
    output_path = (candidate if candidate.is_absolute() else resolved_cwd / candidate).resolve()
    if not output_path.is_relative_to(resolved_cwd):
        raise ValueError("CSV output path must stay within the launch directory")
    if output_path.suffix.lower() != ".csv":
        raise ValueError("CSV output path must end with .csv")
    if output_path.parent != resolved_cwd:
        raise ValueError("CSV output path must be a direct file in the launch directory")

    stem = output_path.stem
    if not stem or "/" in stem or "\\" in stem or ".." in stem:
        raise ValueError("CSV filename must be a safe CSV filename stem")
    identifier_error = validate_identifier(stem, "CSV filename")
    if identifier_error:
        raise ValueError(identifier_error)
    return output_path


def safe_csv_path(launch_cwd: Path, table: str) -> Path:
    """Build a CSV path whose plain filename stem cannot escape ``launch_cwd``."""
    if not table or "/" in table or "\\" in table or ".." in table:
        raise ValueError("Table name must be a safe CSV filename stem")
    identifier_error = validate_identifier(table, "Table name")
    if identifier_error:
        raise ValueError(identifier_error)
    return resolve_csv_output_path(launch_cwd, f"{table}.csv")


def detect_source(sql_text: str) -> str:
    has_start = "{date_inicio}" in sql_text
    has_end = "{date_fim}" in sql_text
    return "SqlTemplate" if has_start and has_end else "SqlFile"


def template_is_complete(sql_text: str) -> bool:
    return "{date_inicio}" in sql_text and "{date_fim}" in sql_text


def is_malformed_template(sql_text: str) -> bool:
    """True when only one of `{date_inicio}` / `{date_fim}` appears - a likely typo."""
    has_start = "{date_inicio}" in sql_text
    has_end = "{date_fim}" in sql_text
    return has_start != has_end


_DDL_LEADERS = ("create", "drop", "insert", "alter", "truncate", "merge")


def is_self_contained_ddl(sql_text: str) -> bool:
    """True when ``sql_text`` already begins with its own DDL/DML statement.

    A ``SqlFile -> Table`` job normally holds a bare ``SELECT`` that we wrap in
    ``DROP/CREATE TABLE ... AS``. If the file instead already opens with
    ``CREATE``/``INSERT``/etc., wrapping it again produces invalid nested DDL,
    so callers should write/preview it verbatim. Leading ``--`` line comments
    and ``/* ... */`` block comments are skipped before inspecting the first
    keyword; a leading ``WITH`` CTE is treated as a SELECT (wrappable).
    """
    remaining = sql_text.lstrip()
    while remaining:
        if remaining.startswith("--"):
            _, _, remaining = remaining.partition("\n")
            remaining = remaining.lstrip()
        elif remaining.startswith("/*"):
            _, _, remaining = remaining.partition("*/")
            remaining = remaining.lstrip()
        else:
            break
    first = remaining[:16].lower()
    return any(first.startswith(leader) for leader in _DDL_LEADERS)


def table_wrapper(sql_text: str, schema: str, table_name: str, user: str) -> str:
    prefix = schema.split("_", 1)[0]
    full_table = f"{schema}.{table_name}"
    return (
        f"DROP TABLE IF EXISTS {full_table};\n"
        f"CREATE TABLE {full_table}\n"
        "STORED AS PARQUET\n"
        f"LOCATION '/das/{prefix}/enc/{user}/{table_name}'\n"
        "AS\n"
        f"{sql_text.strip()}\n"
    )


def month_range(start: date, end: date) -> list[date]:
    months = []
    current = start.replace(day=1)
    while current <= end:
        months.append(current)
        year = current.year + (current.month // 12)
        month = (current.month % 12) + 1
        current = current.replace(year=year, month=month)
    return months


def monthly_preview(
    sql_template: str, schema: str, table_name: str, start_iso: str, end_iso: str
) -> str:
    if DATE_INICIO_TOKEN not in sql_template or DATE_FIM_TOKEN not in sql_template:
        raise ValueError("Monthly SQL template must include {date_inicio} and {date_fim} tokens.")
    start = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end = datetime.strptime(end_iso, "%Y-%m-%d").date()
    # Metadata lines are SQL comments so the preview tokenizes cleanly.
    lines = [f"-- Monthly partitions for {schema}.{table_name}:"]
    for month in month_range(start, end):
        last_day = calendar.monthrange(month.year, month.month)[1]
        month_end = month.replace(day=last_day)
        dt_ano_mes = month.strftime("%Y%m")
        resolved = (
            sql_template.replace(DATE_INICIO_TOKEN, str(month))
            .replace(DATE_FIM_TOKEN, str(month_end))
            .strip()
        )
        lines.extend(
            [
                "",
                f"-- {schema}.{table_name}_temp_{dt_ano_mes}",
                f"-- date_inicio={month}  date_fim={month_end}",
                resolved,
            ]
        )
    return "\n".join(lines)


def to_orchestrator_date(iso_date: str) -> str:
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return parsed.strftime("%m/%d/%Y")


def from_orchestrator_date(orchestrator_date: str) -> str:
    """Inverse of :func:`to_orchestrator_date`: ``MM/DD/YYYY`` -> ``YYYY-MM-DD``.

    Returns the input unchanged when it is not in the expected orchestrator
    format, so clone prefill degrades gracefully on hand-edited manifests.
    """
    try:
        return datetime.strptime(orchestrator_date, "%m/%d/%Y").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return orchestrator_date


def validate_date_range(start_iso: str, end_iso: str) -> str | None:
    """Return an error message for a bad template date range, else ``None``.

    Guards the ``SqlTemplate`` launch/preview path, where unchecked input is
    fed to ``datetime.strptime`` and would otherwise raise mid-flight.
    """
    try:
        start = datetime.strptime(start_iso, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "Start date must be YYYY-MM-DD"
    try:
        end = datetime.strptime(end_iso, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "End date must be YYYY-MM-DD"
    if start > end:
        return "Start date must not be after end date"
    return None

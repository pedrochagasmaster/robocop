"""Reading a Job's Result: the CSV its Csv Destination wrote.

Deep module: the orchestrator exports with impala-shell's ``--delimited``
mode, which writes a header row and comma-separated fields but never quotes or
escapes them (ADR-0010). Quote handling is therefore disabled here, and a row
whose field count disagrees with the header is an error rather than something to
pad or skip.

Nothing in this module is notebook-specific: any Job with a CSV Destination has
a Result, wherever it was launched from.
"""

from __future__ import annotations

import csv
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:  # Optional: Analysts get pandas from the shared runtime, or not at all.
    import pandas
except ImportError:
    pandas = None

# impala-shell --delimited --print_header --output_delimiter=,
DELIMITER = ","
QUOTING = csv.QUOTE_NONE


class ResultError(Exception):
    """A Job's Result could not be read."""


class MissingResultError(ResultError):
    """The Job has no Result file to read."""


class ResultParseError(ResultError):
    """The Result file does not parse as the exported CSV it claims to be."""


def resolve_result_path(path: str | Path | None) -> Path:
    """Return an existing Result path or explain what is missing."""
    if not path:
        raise MissingResultError(
            "This Job has no CSV Result. Relaunch with destination='Csv' or "
            "'Table+Csv', or read the table with Dispatch.table(...)."
        )
    resolved = Path(path)
    if not resolved.is_file():
        raise MissingResultError(f"Result file is missing: {resolved}")
    return resolved


def read_columns(path: str | Path | None) -> list[str]:
    """Return the Result's column names (empty when the export wrote nothing)."""
    resolved = resolve_result_path(path)
    with resolved.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for row in _reader(handle):
            return row
    return []


def iter_rows(path: str | Path | None) -> Iterator[dict[str, str]]:
    """Yield Result rows as dicts, strictly: a ragged row raises.

    Field counts are compared against the header on every line, so a value
    containing a comma or newline fails loudly instead of shifting columns.
    """
    resolved = resolve_result_path(path)
    with resolved.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        rows = _reader(handle)
        header = next(rows, None)
        if header is None:
            return
        width = len(header)
        for line_number, row in enumerate(rows, start=2):
            if not row:
                continue
            if len(row) != width:
                raise ResultParseError(
                    f"{resolved}: line {line_number} has {len(row)} fields but the header "
                    f"has {width}. The export does not quote fields, so a value containing "
                    f"{DELIMITER!r} or a newline splits the row. Remove the delimiter in SQL "
                    "(for example with regexp_replace) and rerun."
                )
            yield dict(zip(header, row))


def read_rows(path: str | Path | None) -> list[dict[str, str]]:
    """Return every Result row as a dict."""
    return list(iter_rows(path))


def validate(path: str | Path | None) -> int:
    """Check every line's field count against the header; return the row count.

    Streams the file without materialising rows, so it is cheap enough to run
    before handing a Result to pandas.
    """
    rows = 0
    for _row in iter_rows(path):
        rows += 1
    return rows


def to_dataframe(path: str | Path | None, *, strict: bool = True, **read_csv_kwargs: Any) -> Any:
    """Return the Result as a ``pandas.DataFrame``.

    Keyword arguments are passed to ``pandas.read_csv``, so callers can supply
    ``dtype``, ``parse_dates``, or ``nrows``. Quoting stays disabled unless
    overridden, matching what impala-shell actually wrote.

    ``strict`` (the default) scans the file first, because pandas does not fail
    on an ambiguous export: a line with too many fields silently becomes an
    index column, and a line with too few is padded with ``NaN``. Pass
    ``strict=False`` to skip the extra pass over a very large Result and accept
    that risk.
    """
    resolved = resolve_result_path(path)
    if pandas is None:
        raise ResultError(
            "pandas is not installed in this runtime, so Results cannot be loaded into a "
            "DataFrame. Ask the Release Operator to add pandas to the shared runtime, or "
            "use rows() / columns instead."
        )
    if strict:
        validate(resolved)
    options: dict[str, Any] = {"quoting": QUOTING, "sep": DELIMITER, "index_col": False}
    options.update(read_csv_kwargs)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", pandas.errors.ParserWarning)
            return pandas.read_csv(resolved, **options)
    except pandas.errors.EmptyDataError:
        return pandas.DataFrame()
    except (pandas.errors.ParserError, pandas.errors.ParserWarning) as exc:
        raise ResultParseError(
            f"{resolved}: pandas could not parse the exported CSV ({exc}). The export does "
            f"not quote fields, so a value containing {DELIMITER!r} or a newline splits the "
            "row. Use rows() to find the offending line."
        ) from exc


def _reader(handle: Any) -> Any:
    return csv.reader(handle, delimiter=DELIMITER, quoting=QUOTING)

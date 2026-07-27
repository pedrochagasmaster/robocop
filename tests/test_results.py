"""Tests for reading a Job's Result (``dispatch/results.py``).

The export is written by impala-shell in ``--delimited`` mode, which never
quotes or escapes fields (ADR-0010). These tests pin that contract: quoting is
off, a ragged line is an error, and nothing is ever silently dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dispatch import results

SCR_DOWNLOAD = Path(__file__).resolve().parents[1] / "scr" / "download_to_csv.py"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestExportContract:
    """What the orchestrator writes must be what this module expects to read."""

    def test_delimiter_matches_the_orchestrator_export(self) -> None:
        source = SCR_DOWNLOAD.read_text(encoding="utf-8")
        assert f"--output_delimiter={results.DELIMITER}" in source
        assert "--print_header" in source

    def test_quoting_is_disabled_because_the_export_does_not_quote(self) -> None:
        source = SCR_DOWNLOAD.read_text(encoding="utf-8")
        assert "--delimited" in source
        assert results.QUOTING == 3  # csv.QUOTE_NONE


class TestReadRows:
    def test_reads_header_and_rows(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n1,mock\n2,other\n")

        assert results.read_columns(path) == ["id", "value"]
        assert results.read_rows(path) == [
            {"id": "1", "value": "mock"},
            {"id": "2", "value": "other"},
        ]

    def test_header_only_result_has_no_rows(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n")

        assert results.read_columns(path) == ["id", "value"]
        assert results.read_rows(path) == []

    def test_empty_file_has_no_columns(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "")

        assert results.read_columns(path) == []
        assert results.read_rows(path) == []

    def test_quotes_are_literal_data(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", 'id,value\n1,"quoted"\n')

        assert results.read_rows(path) == [{"id": "1", "value": '"quoted"'}]

    def test_extra_field_raises_with_the_line_number(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n1,a,b\n")

        with pytest.raises(results.ResultParseError, match="line 2 has 3 fields"):
            results.read_rows(path)

    def test_missing_field_also_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n1\n")

        with pytest.raises(results.ResultParseError, match="line 2 has 1 fields"):
            results.read_rows(path)

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n1,mock\n\n")

        assert results.read_rows(path) == [{"id": "1", "value": "mock"}]


class TestMissingResults:
    def test_no_path_explains_the_destination_requirement(self) -> None:
        with pytest.raises(results.MissingResultError, match="destination='Csv'"):
            results.resolve_result_path(None)

    def test_absent_file_names_the_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.csv"
        with pytest.raises(results.MissingResultError, match=re.escape(str(missing))):
            results.resolve_result_path(missing)

    def test_missing_result_is_a_result_error(self) -> None:
        assert issubclass(results.MissingResultError, results.ResultError)
        assert issubclass(results.ResultParseError, results.ResultError)


class TestDataFrame:
    def test_types_are_inferred_by_pandas(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n1,mock\n2,other\n")

        frame = results.to_dataframe(path)

        assert list(frame.columns) == ["id", "value"]
        assert frame["id"].tolist() == [1, 2]
        assert frame.shape == (2, 2)

    def test_read_csv_kwargs_reach_pandas(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n1,mock\n2,other\n")

        frame = results.to_dataframe(path, dtype={"id": "string"}, nrows=1)

        assert frame["id"].tolist() == ["1"]

    def test_empty_export_is_an_empty_frame(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "")

        assert results.to_dataframe(path).empty

    def test_ragged_export_raises_result_parse_error(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n1,a,b\n")

        with pytest.raises(results.ResultParseError, match="line 2 has 3 fields"):
            results.to_dataframe(path)

    def test_short_row_raises_instead_of_being_padded(self, tmp_path: Path) -> None:
        """pandas pads a short line with NaN; strict mode refuses to."""
        path = _write(tmp_path / "r.csv", "id,value\n1\n")

        with pytest.raises(results.ResultParseError, match="line 2 has 1 fields"):
            results.to_dataframe(path)

    def test_non_strict_read_leaves_pandas_to_its_own_devices(self, tmp_path: Path) -> None:
        """strict=False skips the scan, so a long line becomes pandas' problem."""
        path = _write(tmp_path / "r.csv", "id,value\n1,a,b\n")

        with pytest.raises(results.ResultParseError, match="could not parse"):
            results.to_dataframe(path, strict=False)

    def test_validate_returns_the_row_count(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n1,a\n2,b\n")

        assert results.validate(path) == 2

    def test_without_pandas_the_error_points_at_rows(self, tmp_path: Path, monkeypatch) -> None:
        path = _write(tmp_path / "r.csv", "id,value\n1,mock\n")
        monkeypatch.setattr(results, "pandas", None)

        with pytest.raises(results.ResultError, match="rows()"):
            results.to_dataframe(path)

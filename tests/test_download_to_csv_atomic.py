"""Regression tests for atomic CSV publication in the production export script."""

from __future__ import annotations

import sys
from pathlib import Path

SCR_DIR = Path(__file__).resolve().parents[1] / "scr"
if str(SCR_DIR) not in sys.path:
    sys.path.insert(0, str(SCR_DIR))

import download_to_csv  # noqa: E402


def test_export_writes_to_temp_sibling_then_publishes_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    output_file = tmp_path / "export.csv"
    output_file.write_text("previous complete csv\n", encoding="utf-8")
    observed_output_targets: list[Path] = []
    observed_previous_content: list[str] = []

    def fake_run_impala_shell(command: list[str], *, pool: str = "") -> tuple[int, bytes, bytes]:
        target = Path(command[command.index("-o") + 1])
        observed_output_targets.append(target)
        observed_previous_content.append(output_file.read_text(encoding="utf-8"))
        target.write_text("new complete csv\n", encoding="utf-8")
        return 0, b"ok", b""

    monkeypatch.setattr(download_to_csv, "run_impala_shell", fake_run_impala_shell)

    assert download_to_csv.run_export_on_impala("select 1", str(output_file)) is True

    temp_target = observed_output_targets[0]
    assert temp_target.parent == output_file.parent
    assert temp_target != output_file
    assert temp_target.name.startswith(f".{output_file.name}.")
    assert observed_previous_content == ["previous complete csv\n"]
    assert output_file.read_text(encoding="utf-8") == "new complete csv\n"
    assert not temp_target.exists()


def test_export_failure_removes_temp_file_and_preserves_existing_csv(
    tmp_path: Path, monkeypatch
) -> None:
    output_file = tmp_path / "export.csv"
    output_file.write_text("previous complete csv\n", encoding="utf-8")
    observed_output_targets: list[Path] = []

    def fake_run_impala_shell(command: list[str], *, pool: str = "") -> tuple[int, bytes, bytes]:
        target = Path(command[command.index("-o") + 1])
        observed_output_targets.append(target)
        target.write_text("partial csv\n", encoding="utf-8")
        return 1, b"", b"Admission rejected: queue is full"

    monkeypatch.setattr(download_to_csv, "run_impala_shell", fake_run_impala_shell)

    assert download_to_csv.run_export_on_impala("select 1", str(output_file)) is False

    temp_target = observed_output_targets[0]
    assert output_file.read_text(encoding="utf-8") == "previous complete csv\n"
    assert not temp_target.exists()


def test_publish_failure_removes_temp_file_and_preserves_existing_csv(
    tmp_path: Path, monkeypatch
) -> None:
    output_file = tmp_path / "export.csv"
    output_file.write_text("previous complete csv\n", encoding="utf-8")
    observed_output_targets: list[Path] = []

    def fake_run_impala_shell(command: list[str], *, pool: str = "") -> tuple[int, bytes, bytes]:
        target = Path(command[command.index("-o") + 1])
        observed_output_targets.append(target)
        target.write_text("new complete csv\n", encoding="utf-8")
        return 0, b"ok", b""

    def fail_replace(src: str, dst: str) -> None:
        raise PermissionError(f"cannot publish {src} to {dst}")

    monkeypatch.setattr(download_to_csv, "run_impala_shell", fake_run_impala_shell)
    monkeypatch.setattr(download_to_csv.os, "replace", fail_replace)

    try:
        download_to_csv.run_export_on_impala("select 1", str(output_file))
    except PermissionError:
        pass
    else:
        raise AssertionError("expected publish failure to be propagated")

    temp_target = observed_output_targets[0]
    assert output_file.read_text(encoding="utf-8") == "previous complete csv\n"
    assert not temp_target.exists()


def test_communicate_failure_removes_temp_file_and_preserves_existing_csv(
    tmp_path: Path, monkeypatch
) -> None:
    output_file = tmp_path / "export.csv"
    output_file.write_text("previous complete csv\n", encoding="utf-8")
    observed_output_targets: list[Path] = []

    def fake_run_impala_shell(command: list[str], *, pool: str = "") -> tuple[int, bytes, bytes]:
        target = Path(command[command.index("-o") + 1])
        observed_output_targets.append(target)
        target.write_text("partial csv\n", encoding="utf-8")
        raise RuntimeError("communicate failed")

    monkeypatch.setattr(download_to_csv, "run_impala_shell", fake_run_impala_shell)

    try:
        download_to_csv.run_export_on_impala("select 1", str(output_file))
    except RuntimeError as exc:
        assert str(exc) == "communicate failed"
    else:
        raise AssertionError("expected communicate failure to be propagated")

    temp_target = observed_output_targets[0]
    assert output_file.read_text(encoding="utf-8") == "previous complete csv\n"
    assert not temp_target.exists()

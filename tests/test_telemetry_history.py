"""Historical exports must partition events without losing boundary records."""

import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dispatch_telemetry_history import export_history, main


def rows(output, name):
    with (output / f"interval_{name}.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_history_partitions_boundaries_and_aggregates_all_reports(tmp_path):
    root = tmp_path / "telemetry"
    users = root / "users"
    users.mkdir(parents=True)
    events = [
        {"ts": "2026-01-02T00:00:00Z", "event": "session_start"},
        {"ts": "2026-01-03T00:00:00Z", "event": "session_start"},
        {"ts": "2026-01-03T00:00:00Z", "event": "screen_view", "props": {"screen": "help"}},
        {
            "ts": "2026-01-04T00:00:00+02:00",
            "event": "job_launched",
            "props": {"source": "SqlFile", "destination": "Csv"},
        },
        {"ts": "2026-01-04T12:00:00Z", "event": "launch_refused", "props": {"reason": "slot_cap"}},
        {"ts": "2026-01-05T00:00:00Z", "event": "job_launched"},
        {"ts": "2026-01-06T00:00:00Z", "event": "job_launched"},
    ]
    text = "\n".join(json.dumps({**e, "user": "alice", "session_id": "same"}) for e in events)
    (users / "alice.jsonl").write_text(text + '\ninvalid\n[]\n{"ts": null}\n', encoding="utf-8")
    # A private copy next to the shared rollup must not be counted twice.
    (root / "events.jsonl").write_text(text, encoding="utf-8")
    output = tmp_path / "output"
    assert export_history(
        root, output, bucket_days=2, as_of=datetime(2026, 1, 5, tzinfo=timezone.utc)
    ) == (5, 3)
    summary = rows(output, "summary")
    assert [r["events"] for r in summary] == ["1", "4"]
    assert [r["sessions"] for r in summary] == ["1", "1"]
    assert summary[0]["interval_end"] == summary[1]["interval_start"]
    assert rows(output, "users")[1]["last_seen"] == "2026-01-04T12:00:00Z"
    assert rows(output, "screens")[0]["screen"] == "help"
    assert rows(output, "launches")[0]["source"] == "SqlFile"
    assert rows(output, "refusals")[0]["reason"] == "slot_cap"


def test_max_days_clips_oldest_bucket_and_keeps_empty_intervals(tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_text(
        "\n".join(
            json.dumps({"ts": ts, "event": "session_start"})
            for ts in ["2026-01-01T23:59:59Z", "2026-01-02T00:00:00Z"]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "output"
    assert export_history(
        source, output, bucket_days=2, max_days=3, as_of=datetime(2026, 1, 5, tzinfo=timezone.utc)
    ) == (1, 0)
    summary = rows(output, "summary")
    assert summary[0]["interval_start"] == "2026-01-02T00:00:00Z"
    assert [r["events"] for r in summary] == ["1", "0"]


def test_empty_input_writes_five_headers(tmp_path):
    export_history(tmp_path, tmp_path / "output")
    for name in ("summary", "users", "screens", "launches", "refusals"):
        assert rows(tmp_path / "output", name) == []


@pytest.mark.parametrize("option", ["--bucket-days", "--max-days"])
def test_cli_rejects_non_positive_intervals(option):
    with pytest.raises(SystemExit) as exc:
        main([option, "0"])
    assert exc.value.code == 2


def test_missing_input_fails_without_creating_output(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        export_history(tmp_path / "missing", tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_standalone_cli_runs_outside_checkout(tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_text(
        json.dumps(
            {
                "ts": "2026-01-02T00:00:00Z",
                "event": "job_launched",
                "user": "alice",
                "props": {"source": "SQL, file", "destination": "Csv"},
            }
        ),
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "dispatch_telemetry_history.py"
    result = subprocess.run(
        [sys.executable, str(script), "--dir", str(source), "--as-of", "2026-01-05T00:00:00Z"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Exported 1 events" in result.stdout
    assert rows(tmp_path / "dispatch-telemetry-history", "launches")[0]["source"] == "SQL, file"

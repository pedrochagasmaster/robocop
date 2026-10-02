from __future__ import annotations

import csv
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "tools" / "telemetry_history.py"
SPEC = importlib.util.spec_from_file_location("telemetry_history", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
telemetry_history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(telemetry_history)


def _write_events(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_bucket_index_is_non_overlapping() -> None:
    anchor = datetime(2026, 10, 3, tzinfo=timezone.utc)

    assert telemetry_history.bucket_index(
        datetime(2026, 10, 2, tzinfo=timezone.utc), anchor, 30
    ) == 0
    assert telemetry_history.bucket_index(
        datetime(2026, 9, 3, tzinfo=timezone.utc), anchor, 30
    ) == 1
    assert telemetry_history.bucket_index(
        datetime(2026, 8, 4, tzinfo=timezone.utc), anchor, 30
    ) == 2


def test_main_exports_historical_csvs_and_honors_exact_max_days(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "telemetry"
    output = tmp_path / "history"
    events = [
        {
            "ts": "2026-10-02T12:00:00Z",
            "event": "session_start",
            "user": "alice",
            "session_id": "s1",
            "props": {},
        },
        {
            "ts": "2026-10-02T12:05:00Z",
            "event": "screen_view",
            "user": "alice",
            "session_id": "s1",
            "props": {"screen": "overview"},
        },
        {
            "ts": "2026-09-02T12:00:00Z",
            "event": "job_launched",
            "user": "bob",
            "session_id": "s2",
            "props": {"source": "SqlFile", "destination": "Csv"},
        },
        {
            "ts": "2026-08-28T00:00:00Z",
            "event": "launch_refused",
            "user": "old",
            "session_id": "s3",
            "props": {"reason": "slot_cap"},
        },
    ]
    _write_events(root / "users" / "events.jsonl", events)

    monkeypatch.setattr(
        "sys.argv",
        [
            "telemetry_history.py",
            "--root",
            str(root),
            "--output",
            str(output),
            "--anchor",
            "2026-10-02",
            "--bucket-days",
            "30",
            "--max-days",
            "35",
        ],
    )

    assert telemetry_history.main() == 0

    summary = _read_csv(output / "interval_summary.csv")
    assert [(row["bucket_index"], row["events"]) for row in summary] == [
        ("0", "2"),
        ("1", "1"),
    ]

    users = _read_csv(output / "interval_users.csv")
    assert {row["user"] for row in users} == {"alice", "bob"}

    screens = _read_csv(output / "interval_screens.csv")
    assert screens[0]["screen"] == "overview"
    assert screens[0]["views"] == "1"

    launches = _read_csv(output / "interval_launches.csv")
    assert launches[0]["source"] == "SqlFile"
    assert launches[0]["destination"] == "Csv"
    assert launches[0]["jobs_launched"] == "1"

    assert _read_csv(output / "interval_refusals.csv") == []

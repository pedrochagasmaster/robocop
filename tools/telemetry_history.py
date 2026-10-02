#!/usr/bin/env python3
"""
Build non-overlapping historical datasets from Dispatch telemetry JSONL.

Defaults:
  telemetry root: /ads_storage/dispatch/telemetry
  bucket size:    30 days
  output dir:     ./dispatch-telemetry-history

Outputs:
  interval_summary.csv
  interval_users.csv
  interval_screens.csv
  interval_launches.csv
  interval_refusals.csv

Example:
  python tools/telemetry_history.py
  python tools/telemetry_history.py --bucket-days 30 --max-days 365
  python tools/telemetry_history.py --root /path/to/telemetry --output history
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_ROOT = Path("/ads_storage/dispatch/telemetry")
DEFAULT_OUTPUT = Path("dispatch-telemetry-history")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build non-overlapping historical CSV datasets from Dispatch telemetry."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Telemetry root (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--bucket-days",
        type=int,
        default=30,
        help="Number of days per non-overlapping bucket (default: 30)",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=None,
        help="Optional maximum lookback in days. Default: all available history.",
    )
    parser.add_argument(
        "--anchor",
        type=str,
        default=None,
        help=(
            "Optional UTC anchor timestamp/date for the newest bucket end, e.g. "
            "'2026-10-02' or '2026-10-02T15:00:00Z'. Default: current UTC time."
        ),
    )
    return parser.parse_args()


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def parse_anchor(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)

    text = value.strip()
    if len(text) == 10:
        # A date means end-of-day UTC so that the named date is included.
        dt = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        return dt + timedelta(days=1)

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def telemetry_files(root: Path) -> list[Path]:
    files: list[Path] = []

    users_dir = root / "users"
    if users_dir.is_dir():
        files.extend(sorted(users_dir.glob("*.jsonl")))

    # Also support roots that directly contain JSONL files.
    files.extend(sorted(p for p in root.glob("*.jsonl") if p.is_file()))

    # Avoid duplicate paths.
    return sorted(set(files))


def iter_events(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        print(
                            f"warning: skipping malformed JSON: {path}:{line_no}",
                            file=sys.stderr,
                        )
                        continue
                    if isinstance(item, dict):
                        yield item
        except OSError as exc:
            print(f"warning: could not read {path}: {exc}", file=sys.stderr)


def bucket_index(ts: datetime, anchor: datetime, bucket_days: int) -> int | None:
    age_seconds = (anchor - ts).total_seconds()

    # Ignore future events relative to the chosen anchor.
    if age_seconds < 0:
        return None

    bucket_seconds = bucket_days * 86400
    return int(age_seconds // bucket_seconds)


def bucket_bounds(
    index: int, anchor: datetime, bucket_days: int
) -> tuple[datetime, datetime]:
    end = anchor - timedelta(days=index * bucket_days)
    start = end - timedelta(days=bucket_days)
    return start, end


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()

    if args.bucket_days <= 0:
        raise SystemExit("--bucket-days must be > 0")
    if args.max_days is not None and args.max_days <= 0:
        raise SystemExit("--max-days must be > 0")

    anchor = parse_anchor(args.anchor)
    files = telemetry_files(args.root)

    if not files:
        raise SystemExit(
            f"No telemetry JSONL files found under {args.root}. "
            "Expected files such as <root>/users/<user>.jsonl."
        )

    max_bucket: int | None = None
    if args.max_days is not None:
        max_bucket = (args.max_days - 1) // args.bucket_days

    event_counts: Counter[int] = Counter()
    users_by_bucket: dict[int, set[str]] = defaultdict(set)
    sessions_by_bucket: dict[int, set[str]] = defaultdict(set)
    jobs_by_bucket: Counter[int] = Counter()

    per_user: dict[tuple[int, str], dict[str, Any]] = defaultdict(
        lambda: {
            "events": 0,
            "sessions": set(),
            "jobs_launched": 0,
            "last_seen": None,
        }
    )

    screens: Counter[tuple[int, str]] = Counter()
    launches: Counter[tuple[int, str, str]] = Counter()
    refusals: Counter[tuple[int, str]] = Counter()

    valid_events = 0
    invalid_timestamp_events = 0

    for item in iter_events(files):
        ts = parse_ts(item.get("ts"))
        if ts is None:
            invalid_timestamp_events += 1
            continue

        idx = bucket_index(ts, anchor, args.bucket_days)
        if idx is None:
            continue
        if args.max_days is not None and ts < anchor - timedelta(days=args.max_days):
            continue
        if max_bucket is not None and idx > max_bucket:
            continue

        valid_events += 1
        event_counts[idx] += 1

        user = str(item.get("user") or "")
        session_id = str(item.get("session_id") or "")
        event = str(item.get("event") or "")
        props = item.get("props")
        if not isinstance(props, dict):
            props = {}

        if user:
            users_by_bucket[idx].add(user)

        if event == "session_start" and session_id:
            sessions_by_bucket[idx].add(session_id)

        if event == "job_launched":
            jobs_by_bucket[idx] += 1
            source = str(props.get("source") or "?")
            destination = str(props.get("destination") or "?")
            launches[(idx, source, destination)] += 1
        elif event == "screen_view":
            screen = str(props.get("screen") or "unknown")
            screens[(idx, screen)] += 1
        elif event == "launch_refused":
            reason = str(props.get("reason") or "unknown")
            refusals[(idx, reason)] += 1

        if user:
            key = (idx, user)
            bucket = per_user[key]
            bucket["events"] += 1
            if event == "session_start" and session_id:
                bucket["sessions"].add(session_id)
            if event == "job_launched":
                bucket["jobs_launched"] += 1
            if bucket["last_seen"] is None or ts > bucket["last_seen"]:
                bucket["last_seen"] = ts

    args.output.mkdir(parents=True, exist_ok=True)

    if event_counts:
        observed_max_bucket = max(event_counts)
    else:
        observed_max_bucket = max_bucket if max_bucket is not None else 0

    final_max_bucket = (
        max(observed_max_bucket, max_bucket)
        if max_bucket is not None
        else observed_max_bucket
    )

    summary_rows: list[dict[str, Any]] = []
    for idx in range(final_max_bucket + 1):
        start, end = bucket_bounds(idx, anchor, args.bucket_days)
        summary_rows.append(
            {
                "bucket_index": idx,
                "days_ago_start": idx * args.bucket_days,
                "days_ago_end_exclusive": (idx + 1) * args.bucket_days,
                "period_start_utc": iso(start),
                "period_end_utc_exclusive": iso(end),
                "events": event_counts[idx],
                "users": len(users_by_bucket[idx]),
                "sessions": len(sessions_by_bucket[idx]),
                "jobs_launched": jobs_by_bucket[idx],
            }
        )

    user_rows: list[dict[str, Any]] = []
    for (idx, user), data in sorted(per_user.items()):
        start, end = bucket_bounds(idx, anchor, args.bucket_days)
        user_rows.append(
            {
                "bucket_index": idx,
                "period_start_utc": iso(start),
                "period_end_utc_exclusive": iso(end),
                "user": user,
                "events": data["events"],
                "sessions": len(data["sessions"]),
                "jobs_launched": data["jobs_launched"],
                "last_seen_utc": iso(data["last_seen"]) if data["last_seen"] else "",
            }
        )

    screen_rows: list[dict[str, Any]] = []
    for (idx, screen), count in sorted(screens.items()):
        start, end = bucket_bounds(idx, anchor, args.bucket_days)
        screen_rows.append(
            {
                "bucket_index": idx,
                "period_start_utc": iso(start),
                "period_end_utc_exclusive": iso(end),
                "screen": screen,
                "views": count,
            }
        )

    launch_rows: list[dict[str, Any]] = []
    for (idx, source, destination), count in sorted(launches.items()):
        start, end = bucket_bounds(idx, anchor, args.bucket_days)
        launch_rows.append(
            {
                "bucket_index": idx,
                "period_start_utc": iso(start),
                "period_end_utc_exclusive": iso(end),
                "source": source,
                "destination": destination,
                "jobs_launched": count,
            }
        )

    refusal_rows: list[dict[str, Any]] = []
    for (idx, reason), count in sorted(refusals.items()):
        start, end = bucket_bounds(idx, anchor, args.bucket_days)
        refusal_rows.append(
            {
                "bucket_index": idx,
                "period_start_utc": iso(start),
                "period_end_utc_exclusive": iso(end),
                "reason": reason,
                "refusals": count,
            }
        )

    write_csv(
        args.output / "interval_summary.csv",
        [
            "bucket_index",
            "days_ago_start",
            "days_ago_end_exclusive",
            "period_start_utc",
            "period_end_utc_exclusive",
            "events",
            "users",
            "sessions",
            "jobs_launched",
        ],
        summary_rows,
    )
    write_csv(
        args.output / "interval_users.csv",
        [
            "bucket_index",
            "period_start_utc",
            "period_end_utc_exclusive",
            "user",
            "events",
            "sessions",
            "jobs_launched",
            "last_seen_utc",
        ],
        user_rows,
    )
    write_csv(
        args.output / "interval_screens.csv",
        [
            "bucket_index",
            "period_start_utc",
            "period_end_utc_exclusive",
            "screen",
            "views",
        ],
        screen_rows,
    )
    write_csv(
        args.output / "interval_launches.csv",
        [
            "bucket_index",
            "period_start_utc",
            "period_end_utc_exclusive",
            "source",
            "destination",
            "jobs_launched",
        ],
        launch_rows,
    )
    write_csv(
        args.output / "interval_refusals.csv",
        [
            "bucket_index",
            "period_start_utc",
            "period_end_utc_exclusive",
            "reason",
            "refusals",
        ],
        refusal_rows,
    )

    print(f"Read {len(files)} telemetry file(s)")
    print(f"Anchor UTC: {iso(anchor)}")
    print(f"Bucket size: {args.bucket_days} days")
    print(f"Valid events included: {valid_events}")
    if invalid_timestamp_events:
        print(f"Events skipped for invalid timestamps: {invalid_timestamp_events}")
    print(f"Output: {args.output.resolve()}")
    print("Created:")
    for name in (
        "interval_summary.csv",
        "interval_users.csv",
        "interval_screens.csv",
        "interval_launches.csv",
        "interval_refusals.csv",
    ):
        print(f"  {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

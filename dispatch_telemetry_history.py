#!/usr/bin/env python3
"""Export Dispatch JSONL telemetry into non-overlapping UTC interval CSVs.

Standalone, standard-library-only: copy this file to an Edge Node and run it
with Python 3.10 or later. See --help for input and interval options.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUTPUT_FIELDS = {
    "summary": ["events", "users", "sessions", "jobs_launched", "launch_refusals"],
    "users": ["user", "events", "sessions", "jobs_launched", "last_seen"],
    "screens": ["screen", "count"],
    "launches": ["source", "destination", "count"],
    "refusals": ["reason", "count"],
}
INTERVAL_FIELDS = ["interval_start", "interval_end"]


def parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamps must include a UTC offset")
    return timestamp.astimezone(timezone.utc)


def utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def event_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise ValueError(f"telemetry input does not exist: {root}")
    if (root / "users").is_dir():
        # Shared logs already contain the private events; use only one copy.
        return sorted((root / "users").glob("*.jsonl"))
    if (root / "events.jsonl").is_file():
        return [root / "events.jsonl"]
    return sorted(root.glob("*.jsonl"))


def new_bucket() -> dict:
    return {
        "events": 0,
        "users": {},
        "screens": Counter(),
        "launches": Counter(),
        "refusals": Counter(),
    }


def export_history(
    root: Path,
    output: Path,
    *,
    bucket_days: int = 30,
    max_days: int | None = None,
    as_of: datetime | None = None,
) -> tuple[int, int]:
    """Return (included events, skipped malformed records) after writing five CSVs."""
    if bucket_days <= 0 or (max_days is not None and max_days <= 0):
        raise ValueError("bucket-days and max-days must be positive")
    end = as_of or datetime.now(timezone.utc)
    if end.tzinfo is None:
        raise ValueError("as-of must include a UTC offset")
    end = end.astimezone(timezone.utc)
    width = timedelta(days=bucket_days)
    cutoff = end - timedelta(days=max_days) if max_days is not None else None
    buckets: dict[int, dict] = {}
    skipped = included = 0
    for path in event_files(root):
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError("event must be an object")
                    timestamp = parse_timestamp(item["ts"])
                    event = item["event"]
                    user = item.get("user", "")
                    sid = item.get("session_id", "")
                    if not all(isinstance(v, str) for v in (event, user, sid)):
                        raise ValueError("event, user and session_id must be strings")
                except (ValueError, TypeError, KeyError, AttributeError):
                    skipped += 1
                    continue
                if timestamp >= end or (cutoff is not None and timestamp < cutoff):
                    continue
                # [start, end): an event exactly at start belongs to this interval.
                index = (end - timestamp - timedelta(microseconds=1)) // width
                bucket = buckets.setdefault(index, new_bucket())
                bucket["events"] += 1
                included += 1
                person = bucket["users"].setdefault(
                    user,
                    {"events": 0, "sessions": set(), "jobs_launched": 0, "last_seen": timestamp},
                )
                person["events"] += 1
                person["last_seen"] = max(person["last_seen"], timestamp)
                props = item.get("props")
                props = props if isinstance(props, dict) else {}
                if event == "session_start" and sid:
                    person["sessions"].add(sid)
                elif event == "screen_view":
                    bucket["screens"][str(props.get("screen") or "unknown")] += 1
                elif event == "job_launched":
                    person["jobs_launched"] += 1
                    key = (str(props.get("source") or "?"), str(props.get("destination") or "?"))
                    bucket["launches"][key] += 1
                elif event == "launch_refused":
                    bucket["refusals"][str(props.get("reason") or "unknown")] += 1

    intervals = (
        (max_days + bucket_days - 1) // bucket_days
        if max_days is not None
        else max(buckets, default=-1) + 1
    )
    output.mkdir(parents=True, exist_ok=True)
    # Open one output at a time so even a very long history uses bounded handles.
    for name, fields in OUTPUT_FIELDS.items():
        with (output / f"interval_{name}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=INTERVAL_FIELDS + fields)
            writer.writeheader()
            for index in reversed(range(intervals)):
                interval_end = end - index * width
                interval_start = interval_end - width
                if cutoff is not None:
                    interval_start = max(interval_start, cutoff)
                bounds = dict(zip(INTERVAL_FIELDS, map(utc_text, (interval_start, interval_end))))
                bucket = buckets.get(index) or new_bucket()
                users = bucket["users"]
                if name == "summary":
                    writer.writerow(
                        {
                            **bounds,
                            "events": bucket["events"],
                            "users": len(users),
                            "sessions": sum(len(u["sessions"]) for u in users.values()),
                            "jobs_launched": sum(bucket["launches"].values()),
                            "launch_refusals": sum(bucket["refusals"].values()),
                        }
                    )
                elif name == "users":
                    for user, person in sorted(users.items()):
                        writer.writerow(
                            {
                                **bounds,
                                "user": user,
                                "events": person["events"],
                                "sessions": len(person["sessions"]),
                                "jobs_launched": person["jobs_launched"],
                                "last_seen": utc_text(person["last_seen"]),
                            }
                        )
                else:
                    for key, count in sorted(bucket[name].items()):
                        values = key if name == "launches" else (key,)
                        writer.writerow(
                            {**bounds, **dict(zip(fields[:-1], values)), "count": count}
                        )
    return included, skipped


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path(os.environ.get("DISPATCH_TELEMETRY_DIR") or "/ads_storage/dispatch/telemetry"),
        help="shared telemetry root, users directory, or private JSONL file",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("dispatch-telemetry-history"))
    parser.add_argument("--bucket-days", type=positive_int, default=30)
    parser.add_argument(
        "--max-days", type=positive_int, help="limit history; default: all available"
    )
    parser.add_argument(
        "--as-of",
        type=parse_timestamp,
        help="exclusive interval end (ISO 8601 with UTC offset); default: now",
    )
    args = parser.parse_args(argv)
    try:
        included, skipped = export_history(
            args.dir,
            args.output_dir,
            bucket_days=args.bucket_days,
            max_days=args.max_days,
            as_of=args.as_of,
        )
    except (OSError, ValueError, OverflowError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(f"Exported {included} events to {args.output_dir}")
    if skipped:
        print(f"Skipped {skipped} malformed records", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

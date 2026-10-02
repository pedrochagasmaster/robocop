"""Measure end-to-end Dispatch TUI interaction latency over the real SSH/tmux pane.

The probe is deliberately non-destructive: it never launches or cancels a Job
and never drops a table. It exercises navigation, Browser selection, log search
when a Job is available, and New Job SQL-path typing using a temporary /tmp SQL
file plus the existing DISPATCH_TEST_PREFILL seam.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.prod_tui.robocop_tmux import (
    DEFAULT_CONFIG_PATH,
    ProdTuiConfig,
    SessionGoneError,
    TmuxDriver,
    load_config,
)

DASHBOARD_READY = r"running first|No jobs in the last 7 days|KERBEROS.*RUNNING"
NEW_JOB_READY = r"New Job.*Source|Source.*Destination"
HISTORY_READY = r"Job History"
BROWSER_SHELL_READY = r"Browse Impala Metadata"
BROWSER_LIST_READY = r"\b\d+ tables\b|\(no tables\)|SHOW TABLES failed"
JOB_DETAIL_READY = r"Logs.*Streaming|Logs.*Complete|Job [A-Za-z0-9_-]+"
SEARCH_READY = r"Search log"


def _summary(samples: list[float]) -> dict[str, Any]:
    if not samples:
        return {"n": 0, "samples_ms": []}
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "n": len(samples),
        "median_ms": round(statistics.median(samples), 2),
        "p95_ms": round(ordered[p95_index], 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
        "samples_ms": [round(value, 2) for value in samples],
    }


def _wait_for(
    driver: TmuxDriver,
    pattern: str,
    *,
    timeout: float,
    poll_interval: float,
) -> str:
    target = re.compile(pattern, re.MULTILINE | re.DOTALL)
    deadline = time.perf_counter() + timeout
    last = ""
    while time.perf_counter() < deadline:
        last = driver.capture_screen()
        if target.search(last):
            return last
        time.sleep(poll_interval)
    raise TimeoutError(f"Timed out waiting for {pattern!r}. Last screen:\n{last}")


def _measure(
    driver: TmuxDriver,
    action: Callable[[], None],
    pattern: str,
    *,
    timeout: float,
    poll_interval: float,
) -> tuple[float, str]:
    started = time.perf_counter()
    action()
    screen = _wait_for(driver, pattern, timeout=timeout, poll_interval=poll_interval)
    return (time.perf_counter() - started) * 1000.0, screen


def _ensure_dashboard(driver: TmuxDriver, *, poll_interval: float) -> str:
    screen = driver.capture_screen()
    if re.search(DASHBOARD_READY, screen, re.MULTILINE | re.DOTALL):
        return screen
    driver.return_to_shell()
    if not driver.type_command_confirmed("dispatch"):
        raise RuntimeError("Could not reliably type the dispatch command into the SSH pane")
    return _wait_for(driver, DASHBOARD_READY, timeout=30.0, poll_interval=poll_interval)


def _back_to_dashboard(driver: TmuxDriver, *, poll_interval: float) -> None:
    driver.send_key("Escape")
    _wait_for(driver, DASHBOARD_READY, timeout=10.0, poll_interval=poll_interval)


def _navigation_samples(
    driver: TmuxDriver,
    *,
    key: str,
    target: str,
    samples: int,
    poll_interval: float,
    settle_pattern: str | None = None,
) -> list[float]:
    values: list[float] = []
    for _ in range(samples):
        _ensure_dashboard(driver, poll_interval=poll_interval)
        elapsed, _ = _measure(
            driver,
            lambda: driver.send_key(key),
            target,
            timeout=20.0,
            poll_interval=poll_interval,
        )
        values.append(elapsed)
        if settle_pattern:
            _wait_for(
                driver,
                settle_pattern,
                timeout=35.0,
                poll_interval=max(poll_interval, 0.05),
            )
        _back_to_dashboard(driver, poll_interval=poll_interval)
    return values


def _browser_toggle_samples(
    driver: TmuxDriver, *, samples: int, poll_interval: float
) -> tuple[list[float], str | None]:
    _ensure_dashboard(driver, poll_interval=poll_interval)
    driver.send_key("b")
    screen = _wait_for(
        driver,
        BROWSER_LIST_READY,
        timeout=35.0,
        poll_interval=max(0.03, poll_interval),
    )
    if "(no tables)" in screen or "SHOW TABLES failed" in screen:
        _back_to_dashboard(driver, poll_interval=poll_interval)
        return [], "Browser had no actionable table rows"

    # Browser focus order is schema -> filter -> Load -> Select All -> table.
    for _ in range(4):
        driver.send_key("Tab")
        time.sleep(0.03)

    values: list[float] = []
    try:
        for _ in range(samples):
            elapsed, _ = _measure(
                driver,
                lambda: driver.send_key("x"),
                r"1 selected for drop",
                timeout=5.0,
                poll_interval=poll_interval,
            )
            values.append(elapsed)
            driver.send_key("x")
            _wait_for(
                driver,
                r"Click \[ \] or press X",
                timeout=5.0,
                poll_interval=poll_interval,
            )
    finally:
        _back_to_dashboard(driver, poll_interval=poll_interval)
    return values, None


def _log_search_samples(
    driver: TmuxDriver, *, samples: int, poll_interval: float
) -> tuple[list[float], str | None]:
    screen = _ensure_dashboard(driver, poll_interval=poll_interval)
    if "No jobs in the last 7 days" in screen:
        return [], "No recent Job is available for Job Detail/log-search measurement"
    try:
        driver.send_key("v")
        _wait_for(driver, JOB_DETAIL_READY, timeout=10.0, poll_interval=poll_interval)
    except TimeoutError:
        return [], "Dashboard had no selectable Job for Job Detail"

    driver.send_key("/")
    _wait_for(driver, SEARCH_READY, timeout=5.0, poll_interval=poll_interval)
    values: list[float] = []
    try:
        for index in range(samples):
            driver.send_key("C-u")
            time.sleep(0.25)
            marker = f"zzperf{index:02d}xyz"
            elapsed, _ = _measure(
                driver,
                lambda marker=marker: driver.send_keys(marker, literal=True),
                re.escape(marker),
                timeout=5.0,
                poll_interval=poll_interval,
            )
            values.append(elapsed)
    finally:
        driver.send_key("Enter")
        time.sleep(0.2)
        _back_to_dashboard(driver, poll_interval=poll_interval)
    return values, None


def _new_job_typing_samples(
    driver: TmuxDriver, *, samples: int, poll_interval: float
) -> tuple[list[float], str | None]:
    driver.return_to_shell()
    nonce = uuid.uuid4().hex[:10]
    probe_dir = f"/tmp/dispatch-latency-probe-{nonce}"
    sql_path = f"{probe_dir}/query.sql"
    prefill_path = f"{probe_dir}/prefill.json"
    prefill = json.dumps({"sql_file": sql_path})
    setup = (
        f"mkdir -p {shlex.quote(probe_dir)} && "
        f"printf 'SELECT 1 AS latency_probe;\\n' > {shlex.quote(sql_path)} && "
        f"printf '%s\\n' {shlex.quote(prefill)} > {shlex.quote(prefill_path)}"
    )
    _, code = driver.run_remote(setup, timeout=10.0)
    if code != 0:
        return [], "Could not prepare temporary SQL/prefill files under /tmp"

    values: list[float] = []
    try:
        command = f"DISPATCH_TEST_PREFILL={shlex.quote(prefill_path)} dispatch"
        if not driver.type_command_confirmed(command):
            return [], "Could not reliably launch Dispatch with the prefill seam"
        _wait_for(driver, NEW_JOB_READY, timeout=30.0, poll_interval=poll_interval)
        # With a prefill the SQL picker is hidden: source -> destination -> queue -> SQL Input.
        for _ in range(3):
            driver.send_key("Tab")
            time.sleep(0.03)

        for index in range(samples):
            driver.send_key("C-u")
            driver.send_keys(sql_path, literal=True)
            _wait_for(driver, r"query\.sql", timeout=5.0, poll_interval=poll_interval)
            time.sleep(0.3)
            marker = f".perf{index:02d}xyz"
            elapsed, _ = _measure(
                driver,
                lambda marker=marker: driver.send_keys(marker, literal=True),
                re.escape(marker),
                timeout=5.0,
                poll_interval=poll_interval,
            )
            values.append(elapsed)
    finally:
        try:
            driver.return_to_shell()
            driver.run_remote(f"rm -rf {shlex.quote(probe_dir)}", timeout=10.0)
        except Exception:
            pass
    return values, None


def _capture_overhead(driver: TmuxDriver, samples: int = 12) -> list[float]:
    values = []
    for _ in range(samples):
        started = time.perf_counter()
        driver.capture_screen()
        values.append((time.perf_counter() - started) * 1000.0)
    return values


def _deployment_sha(driver: TmuxDriver) -> str | None:
    try:
        screen, code = driver.run_remote("git rev-parse HEAD", timeout=10.0)
    except Exception:
        return None
    if code != 0:
        return None
    matches = re.findall(r"\b[0-9a-f]{40}\b", screen)
    return matches[-1] if matches else None


def _compare(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    comparison: dict[str, Any] = {}
    base_measurements = baseline.get("measurements", {})
    for name, current in candidate.get("measurements", {}).items():
        previous = base_measurements.get(name, {})
        current_median = current.get("median_ms")
        previous_median = previous.get("median_ms")
        if not isinstance(current_median, (int, float)) or not isinstance(
            previous_median, (int, float)
        ):
            continue
        improvement = (
            ((previous_median - current_median) / previous_median) * 100.0
            if previous_median
            else 0.0
        )
        comparison[name] = {
            "baseline_median_ms": previous_median,
            "candidate_median_ms": current_median,
            "delta_ms": round(current_median - previous_median, 2),
            "improvement_pct": round(improvement, 1),
        }
    return comparison


def run_probe(config: ProdTuiConfig, *, samples: int, poll_ms: float) -> dict[str, Any]:
    driver = TmuxDriver.from_config(config)
    if not driver.session_exists():
        raise RuntimeError(
            f"tmux session {config.session_name!r} is not running. Authenticate it first with "
            f"python -m tools.prod_tui tmux start --config <CONFIG>."
        )
    driver.resize_window(config.terminal_width, config.terminal_height)
    poll_interval = max(0.01, poll_ms / 1000.0)
    _ensure_dashboard(driver, poll_interval=poll_interval)

    measurements: dict[str, Any] = {}
    notes: dict[str, str] = {}
    measurements["capture_overhead_ms"] = _summary(_capture_overhead(driver))
    measurements["new_job_navigation_ms"] = _summary(
        _navigation_samples(
            driver,
            key="n",
            target=NEW_JOB_READY,
            samples=samples,
            poll_interval=poll_interval,
        )
    )
    measurements["history_navigation_ms"] = _summary(
        _navigation_samples(
            driver,
            key="h",
            target=HISTORY_READY,
            samples=samples,
            poll_interval=poll_interval,
        )
    )
    # Browser entry invokes real read-only metadata calls. Cap repetitions to 3.
    measurements["browser_navigation_ms"] = _summary(
        _navigation_samples(
            driver,
            key="b",
            target=BROWSER_SHELL_READY,
            samples=min(samples, 3),
            poll_interval=poll_interval,
            settle_pattern=BROWSER_LIST_READY,
        )
    )

    browser, reason = _browser_toggle_samples(
        driver, samples=samples, poll_interval=poll_interval
    )
    measurements["browser_toggle_ms"] = _summary(browser)
    if reason:
        notes["browser_toggle_ms"] = reason

    log_search, reason = _log_search_samples(driver, samples=samples, poll_interval=poll_interval)
    measurements["log_search_typing_ms"] = _summary(log_search)
    if reason:
        notes["log_search_typing_ms"] = reason

    new_job_typing, reason = _new_job_typing_samples(
        driver, samples=samples, poll_interval=poll_interval
    )
    measurements["new_job_sql_typing_ms"] = _summary(new_job_typing)
    if reason:
        notes["new_job_sql_typing_ms"] = reason

    driver.return_to_shell()
    sha = _deployment_sha(driver)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": config.host,
        "session_name": config.session_name,
        "terminal": {"width": config.terminal_width, "height": config.terminal_height},
        "deployment_sha": sha,
        "samples_requested": samples,
        "poll_ms": poll_ms,
        "measurements": measurements,
        "notes": notes,
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"Host: {report['host']}")
    print(f"Deployment: {report.get('deployment_sha') or 'unknown'}")
    for name, result in report["measurements"].items():
        if result.get("n", 0):
            print(
                f"{name}: median={result['median_ms']:.2f} ms "
                f"p95={result['p95_ms']:.2f} ms n={result['n']}"
            )
        else:
            note = report.get("notes", {}).get(name, "not measured")
            print(f"{name}: SKIPPED ({note})")
    for name, result in report.get("comparison", {}).items():
        print(
            f"compare {name}: {result['baseline_median_ms']:.2f} -> "
            f"{result['candidate_median_ms']:.2f} ms "
            f"({result['improvement_pct']:+.1f}% improvement)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--poll-ms", type=float, default=15.0)
    parser.add_argument("--json-report")
    parser.add_argument(
        "--baseline-report",
        help="Optional previous JSON report; prints per-metric median deltas.",
    )
    args = parser.parse_args(argv)
    if args.samples < 1:
        parser.error("--samples must be >= 1")

    config = load_config(args.config)
    try:
        report = run_probe(config, samples=args.samples, poll_ms=args.poll_ms)
    except (RuntimeError, TimeoutError, SessionGoneError) as exc:
        print(f"Latency probe failed: {exc}")
        return 2

    if args.baseline_report:
        baseline = json.loads(Path(args.baseline_report).read_text(encoding="utf-8"))
        report["comparison"] = _compare(report, baseline)

    report_path = (
        Path(args.json_report)
        if args.json_report
        else Path("tools/prod_tui/reports") / f"latency-{config.session_name}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _print_report(report)
    print(f"JSON report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Non-interactive ``dispatch job`` CLI adapter.

Argparse, human/JSON formatting, and exit codes live here. Domain behavior is
delegated to :mod:`dispatch.job_ops`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from . import job_ops, kerberos, setup_logging
from .job_ops import (
    AdvisorAcknowledgementRequired,
    CancelResult,
    ConfirmationRequired,
    LaunchInputs,
    LaunchResult,
    OperationalError,
    UnknownJobError,
    ValidationError,
    WaitResult,
)

# Stable exit codes for scripting.
EXIT_OK = 0
EXIT_JOB_UNSUCCESSFUL = 1
EXIT_USAGE = 2
EXIT_UNKNOWN_JOB = 3
EXIT_OPERATIONAL = 4


def add_job_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``dispatch job …`` subcommands on the root parser."""
    job_parser = subparsers.add_parser(
        "job",
        help="Launch and supervise Jobs without the interactive TUI.",
        description=(
            "Non-interactive Job launch and supervision. "
            "With no subcommand under job, see: dispatch job --help"
        ),
    )
    job_sub = job_parser.add_subparsers(dest="job_command", required=True)

    launch = job_sub.add_parser(
        "launch",
        help="Validate, admit, and hand off a Job to the detached runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Legal source/destination cells:\n"
            "  SqlFile       -> Table | Csv | Table+Csv\n"
            "  SqlTemplate   -> Table\n"
            "  ExistingTable -> Csv\n"
            "\n"
            "Confirmations: pass --yes for every launch; when Advisor reports\n"
            "error-severity findings, also pass --acknowledge-advisor.\n"
            "\n"
            "Exit codes: 0 ok; 2 validation/usage; 4 operational refusal/handoff."
        ),
    )
    launch.add_argument(
        "--source",
        required=True,
        choices=["SqlFile", "SqlTemplate", "ExistingTable"],
        help="Job source type.",
    )
    launch.add_argument(
        "--destination",
        required=True,
        choices=["Table", "Csv", "Table+Csv"],
        help="Job destination type.",
    )
    launch.add_argument(
        "--sql",
        dest="sql_path",
        default="",
        help="SQL file path (relative paths resolve against the invocation CWD).",
    )
    launch.add_argument(
        "--existing-table",
        default="",
        help="Fully qualified schema.table for ExistingTable -> Csv.",
    )
    launch.add_argument(
        "--schema",
        default="aa_enc",
        help="Destination schema for Table / Table+Csv (default: aa_enc).",
    )
    launch.add_argument(
        "--table",
        default="dispatch_result",
        help="Destination table suffix (EID_ prefix applied) or full EID_name.",
    )
    launch.add_argument("--start-date", default="", help="SqlTemplate start date (YYYY-MM-DD).")
    launch.add_argument("--end-date", default="", help="SqlTemplate end date (YYYY-MM-DD).")
    launch.add_argument(
        "--email",
        default="",
        help="Notification email(s). Defaults empty; orchestrators may still mail.",
    )
    launch.add_argument("--subject", default="Dispatch Job", help="Notification subject.")
    launch.add_argument(
        "--queue",
        default=job_ops.QUEUE_AUTO,
        help=(
            f"Resource Pool selection: '{job_ops.QUEUE_AUTO}' (default) or a comma-separated "
            f"subset of {', '.join(job_ops.QUEUE_ORDER)}. "
            "Stored as params.queue for orchestrator compatibility."
        ),
    )
    launch.add_argument(
        "--yes",
        action="store_true",
        help="Confirm launch without an interactive prompt.",
    )
    launch.add_argument(
        "--acknowledge-advisor",
        action="store_true",
        help="Acknowledge Advisor error-severity findings and launch SQL as written.",
    )
    _add_json_flag(launch)

    list_parser = job_sub.add_parser("list", help="List Jobs after reconciling stale manifests.")
    list_parser.add_argument(
        "--state",
        default=None,
        choices=list(job_ops.JOB_STATES),
        help="Filter by Job state.",
    )
    _add_json_flag(list_parser)

    show = job_sub.add_parser("show", help="Show one Job's reconciled state.")
    show.add_argument("job_id", help="Job ID.")
    _add_json_flag(show)

    logs = job_sub.add_parser("logs", help="Print or follow a Job's run.log.")
    logs.add_argument("job_id", help="Job ID.")
    logs.add_argument(
        "--lines",
        type=int,
        default=50,
        help="Initial number of trailing lines to print (default: 50).",
    )
    logs.add_argument(
        "--follow",
        action="store_true",
        help="Follow until the Job is terminal and remaining log bytes are emitted.",
    )

    wait = job_sub.add_parser("wait", help="Wait until a Job reaches a terminal state.")
    wait.add_argument("job_id", help="Job ID.")
    wait.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Seconds to wait before giving up (default: wait forever).",
    )
    wait.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1).",
    )
    _add_json_flag(wait)
    wait.epilog = (
        "Exit codes: 0 Succeeded; 1 Failed/Cancelled; 3 unknown Job; 4 timeout/operational."
    )

    cancel = job_sub.add_parser(
        "cancel",
        help="Cancel a Pending or Running Job (requires --yes).",
    )
    cancel.add_argument("job_id", help="Job ID.")
    cancel.add_argument(
        "--yes",
        action="store_true",
        required=True,
        help="Confirm cancellation (required).",
    )
    _add_json_flag(cancel)


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Write one JSON document to stdout (diagnostics on stderr).",
    )


def run_job_command(args: argparse.Namespace) -> int:
    """Dispatch a parsed ``job`` subcommand. Returns a process exit code."""
    setup_logging()
    command = args.job_command
    try:
        if command == "launch":
            return _cmd_launch(args)
        if command == "list":
            return _cmd_list(args)
        if command == "show":
            return _cmd_show(args)
        if command == "logs":
            return _cmd_logs(args)
        if command == "wait":
            return _cmd_wait(args)
        if command == "cancel":
            return _cmd_cancel(args)
    except ConfirmationRequired as exc:
        return _fail(EXIT_USAGE, str(exc), json_output=getattr(args, "json_output", False))
    except AdvisorAcknowledgementRequired as exc:
        return _fail(EXIT_USAGE, str(exc), json_output=getattr(args, "json_output", False))
    except ValidationError as exc:
        return _fail(EXIT_USAGE, str(exc), json_output=getattr(args, "json_output", False))
    except UnknownJobError as exc:
        return _fail(EXIT_UNKNOWN_JOB, str(exc), json_output=getattr(args, "json_output", False))
    except OperationalError as exc:
        return _fail(EXIT_OPERATIONAL, str(exc), json_output=getattr(args, "json_output", False))
    raise SystemExit(f"Unknown job command: {command}")


def _cmd_launch(args: argparse.Namespace) -> int:
    launch_cwd = Path.cwd()
    table_name = job_ops.table_name_for_inputs(
        source_type=args.source,
        destination_type=args.destination,
        table_suffix_or_full=args.table,
    )
    inputs = LaunchInputs(
        source_type=args.source,
        destination_type=args.destination,
        launch_cwd=launch_cwd,
        sql_path=args.sql_path,
        existing_table=args.existing_table,
        schema=args.schema,
        table_name=table_name,
        start_date=args.start_date,
        end_date=args.end_date,
        email=args.email or "",
        subject=args.subject,
        queue=args.queue,
    )

    def _announce(plan: job_ops.LaunchPlan) -> None:
        if args.json_output:
            return
        print(job_ops.launch_summary_text(plan), file=sys.stderr)
        errors = plan.analysis.errors()
        if errors:
            print(
                f"Advisor errors: {', '.join(sorted({f.rule_id for f in errors}))}",
                file=sys.stderr,
            )

    result = job_ops.launch_job(
        inputs,
        kerberos_ttl=kerberos.ticket_ttl_seconds_sync(),
        yes=args.yes,
        acknowledge_advisor=args.acknowledge_advisor,
        recheck_ttl=kerberos.ticket_ttl_seconds_sync,
        on_plan=_announce,
    )
    return _emit_launch(result, json_output=args.json_output)


def _cmd_list(args: argparse.Namespace) -> int:
    items = job_ops.list_jobs(state=args.state)
    if args.json_output:
        _print_json({"jobs": [job_ops.job_summary_dict(item) for item in items]})
        return EXIT_OK
    if not items:
        print("No jobs.")
        return EXIT_OK
    header = f"{'ID':<28} {'STATE':<10} {'SOURCE':<14} {'DEST':<10} {'PID':>8} {'EXIT':>5}"
    print(header)
    print("-" * len(header))
    for item in items:
        source = (item.get("source") or {}).get("type", "--")
        dest = (item.get("destination") or {}).get("type", "--")
        pid = item.get("pid")
        exit_code = item.get("exit_code")
        print(
            f"{item['id']:<28} {item['state']:<10} {source:<14} {dest:<10} "
            f"{pid if pid is not None else '--':>8} "
            f"{exit_code if exit_code is not None else '--':>5}"
        )
    return EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    item = job_ops.load_job(args.job_id)
    detail = job_ops.job_detail_dict(item)
    if args.json_output:
        _print_json(detail)
        return EXIT_OK
    summary = job_ops.job_summary_dict(item)
    print(f"Job ID:        {summary['id']}")
    print(f"State:         {summary['state']}")
    print(f"Source:        {summary['source']}  {summary.get('source_detail') or ''}".rstrip())
    print(
        f"Destination:   {summary['destination']}  {summary.get('destination_detail') or ''}".rstrip()
    )
    print(f"Created:       {summary.get('created_at') or '--'}")
    print(f"Started:       {summary.get('started_at') or '--'}")
    print(f"Finished:      {summary.get('finished_at') or '--'}")
    print(f"PID:           {summary.get('pid') if summary.get('pid') is not None else '--'}")
    print(
        f"Exit code:     {summary.get('exit_code') if summary.get('exit_code') is not None else '--'}"
    )
    print(f"User:          {summary.get('user') or '--'}")
    params = summary.get("params") or {}
    if params:
        print("Params:")
        for key in sorted(params):
            print(f"  {key}: {params[key]}")
    return EXIT_OK


def _cmd_logs(args: argparse.Namespace) -> int:
    if args.follow:
        for line in job_ops.follow_logs(args.job_id, lines=args.lines):
            print(line)
        return EXIT_OK
    for line in job_ops.read_log_tail(args.job_id, lines=args.lines):
        print(line)
    return EXIT_OK


def _cmd_wait(args: argparse.Namespace) -> int:
    result = job_ops.wait_job(
        args.job_id,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    return _emit_wait(result, json_output=args.json_output)


def _cmd_cancel(args: argparse.Namespace) -> int:
    result = job_ops.cancel_job(args.job_id, yes=args.yes)
    if result.kind == "not_cancellable":
        raise OperationalError(result.message)
    return _emit_cancel(result, json_output=args.json_output)


def _emit_launch(result: LaunchResult, *, json_output: bool) -> int:
    payload = {
        "job_id": result.job_id,
        "state": result.manifest.get("state"),
        "pid": result.manifest.get("pid"),
    }
    if json_output:
        _print_json(payload)
    else:
        print(result.job_id)
    return EXIT_OK


def _emit_wait(result: WaitResult, *, json_output: bool) -> int:
    item = result.manifest
    payload = {
        "job_id": result.job_id,
        "state": item["state"],
        "exit_code": item.get("exit_code"),
        "timed_out": result.timed_out,
    }
    if json_output:
        _print_json(payload)
    else:
        if result.timed_out:
            print(
                f"Timed out waiting for Job {result.job_id} (state={item['state']})",
                file=sys.stderr,
            )
        else:
            print(f"{result.job_id} {item['state']}")
    if result.timed_out:
        return EXIT_OPERATIONAL
    if item["state"] == "Succeeded":
        return EXIT_OK
    if item["state"] in {"Failed", "Cancelled"}:
        return EXIT_JOB_UNSUCCESSFUL
    return EXIT_OPERATIONAL


def _emit_cancel(result: CancelResult, *, json_output: bool) -> int:
    payload = {
        "job_id": result.job_id,
        "result": result.kind,
        "state": result.manifest.get("state"),
        "exit_code": result.manifest.get("exit_code"),
        "message": result.message,
    }
    if json_output:
        _print_json(payload)
    else:
        print(result.message)
    if result.kind == "reconciled_missing":
        return EXIT_OPERATIONAL
    return EXIT_OK


def _print_json(payload: dict[str, Any], stream: TextIO | None = None) -> None:
    target = sys.stdout if stream is None else stream
    json.dump(payload, target, indent=2, sort_keys=True)
    target.write("\n")


def _fail(code: int, message: str, *, json_output: bool) -> int:
    if json_output:
        _print_json({"error": message, "exit_code": code}, stream=sys.stderr)
    else:
        print(message, file=sys.stderr)
    return code

"""Command-line entry point for Dispatch."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import cli_job, telemetry
from .app import DispatchApp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="dispatch",
        description=(
            "Server-side TUI and non-interactive CLI for launching and supervising Impala Jobs."
        ),
        epilog=(
            "With no subcommand, launches the interactive TUI.\n"
            "Use 'dispatch job --help' for non-interactive Job commands.\n"
            "Use 'dispatch telemetry --help' for offline usage reports."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", title="commands")

    cli_job.add_job_parser(subparsers)

    telemetry_parser = subparsers.add_parser(
        "telemetry",
        help="Show offline usage telemetry (who / how).",
    )
    telemetry_sub = telemetry_parser.add_subparsers(dest="telemetry_command", required=True)

    who_parser = telemetry_sub.add_parser("who", help="List users, sessions, and Job launches.")
    _add_telemetry_common_args(who_parser)

    summary_parser = telemetry_sub.add_parser(
        "summary",
        help="Summarize screens, launch mix, and refusals.",
    )
    _add_telemetry_common_args(summary_parser)
    summary_parser.add_argument(
        "--user",
        default=None,
        help="Limit to one username.",
    )

    args = parser.parse_args(argv)
    if args.command == "telemetry":
        _run_telemetry(args)
        return
    if args.command == "job":
        raise SystemExit(cli_job.run_job_command(args))
    DispatchApp().run()


def _add_telemetry_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look back this many days (default: 30).",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Telemetry root to read (default: shared dir, else private).",
    )


def _run_telemetry(args: argparse.Namespace) -> None:
    root = args.dir
    if root is None:
        shared = telemetry.shared_telemetry_dir()
        if (shared / "users").is_dir() or shared.exists():
            root = shared
        else:
            root = telemetry.private_telemetry_dir()

    if args.telemetry_command == "who":
        report = telemetry.who(days=args.days, root=root)
        print(telemetry.format_who(report), end="")
        return
    if args.telemetry_command == "summary":
        report = telemetry.summary(days=args.days, root=root, user=args.user)
        print(telemetry.format_summary(report), end="")
        return
    raise SystemExit(f"Unknown telemetry command: {args.telemetry_command}")


if __name__ == "__main__":
    main()

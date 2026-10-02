"""Entry point for the tools.prod_tui CLI.

Provides a unified interface to the production TUI test harness.
"""
from __future__ import annotations

import sys


def _print_usage() -> None:
    print("Usage: python -m tools.prod_tui <command> [args...]")
    print("")
    print("Commands:")
    print("  preflight  Check DNS and TCP reachability before SSH/auth")
    print("  tmux       Manage the local tmux session (start, stop, send, capture)")
    print("  smoke      Run Level 1 and 2 smoke tests")
    print("  latency    Measure real SSH/TUI interaction latency without launching Jobs")
    print("  job        Run the Level 3 controlled job")
    print("  level      Run Level 4-6 controlled scenarios (--level 4|5|6)")
    print("  deploy     Update one Edge node to an exact deployment commit")
    print("  drift      Compare runtime-critical files against an expected commit")


def main() -> int:
    if len(sys.argv) < 2:
        _print_usage()
        return 1

    command = sys.argv[1]
    argv = sys.argv[2:]

    if command in {"-h", "--help"}:
        _print_usage()
        return 0

    if command == "preflight":
        from tools.prod_tui.preflight import main as preflight_main

        return preflight_main(argv)
    elif command == "tmux":
        from tools.prod_tui.robocop_tmux import main as tmux_main

        return tmux_main(argv)
    elif command == "smoke":
        from tools.prod_tui.smoke_test import main as smoke_main

        return smoke_main(argv)
    elif command == "latency":
        from tools.prod_tui.latency_probe import main as latency_main

        return latency_main(argv)
    elif command == "job":
        from tools.prod_tui.controlled_job import main as job_main

        return job_main(argv)
    elif command == "level":
        from tools.prod_tui.levels import main as level_main

        return level_main(argv)
    elif command == "deploy":
        from tools.prod_tui.deploy import main as deploy_main

        return deploy_main(argv)
    elif command == "drift":
        from tools.prod_tui.drift import main as drift_main

        return drift_main(argv)
    else:
        print(f"Unknown command: {command}")
        print("Available commands: preflight, tmux, smoke, latency, job, level, deploy, drift")
        return 1


if __name__ == "__main__":
    sys.exit(main())

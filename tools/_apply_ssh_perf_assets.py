from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"pattern not found in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Keep the implementation-specific debounce regression aligned with the new
# worker boundary and 150 ms coalescing interval.
replace(
    "tests/test_new_features.py",
    '''                calls = 0
                original = screen._update_validation_summary

                def counting_update() -> None:
                    nonlocal calls
                    calls += 1
                    original()

                screen._update_validation_summary = counting_update  # type: ignore[method-assign]
                scheduled: list[tuple[float, object, _TimerStub]] = []
''',
    '''                calls = 0

                def counting_start() -> None:
                    nonlocal calls
                    calls += 1

                screen._start_validation_worker = counting_start  # type: ignore[method-assign]
                scheduled: list[tuple[float, object, _TimerStub]] = []
''',
)
replace(
    "tests/test_new_features.py",
    '''                assert [item[0] for item in scheduled] == [0.2, 0.2]
''',
    '''                assert [item[0] for item in scheduled] == [0.15, 0.15]
''',
)

# Register the read-only production latency probe in the unified harness CLI.
replace(
    "tools/prod_tui/__main__.py",
    '''    print("  drift      Compare runtime-critical files against an expected commit")
''',
    '''    print("  drift      Compare runtime-critical files against an expected commit")
    print("  latency    Measure real SSH/TUI interaction latency without launching Jobs")
''',
)
replace(
    "tools/prod_tui/__main__.py",
    '''    elif command == "drift":
        from tools.prod_tui.drift import main as drift_main
        return drift_main(argv)
    else:
''',
    '''    elif command == "drift":
        from tools.prod_tui.drift import main as drift_main
        return drift_main(argv)
    elif command == "latency":
        from tools.prod_tui.latency_probe import main as latency_main
        return latency_main(argv)
    else:
''',
)
replace(
    "tools/prod_tui/__main__.py",
    '''        print("Available commands: preflight, tmux, smoke, job, level, deploy, drift")
''',
    '''        print("Available commands: preflight, tmux, smoke, job, level, deploy, drift, latency")
''',
)
replace(
    "tools/prod_tui/tests/test_cli.py",
    '''    assert "deploy" in out
    assert "drift" in out
''',
    '''    assert "deploy" in out
    assert "drift" in out
    assert "latency" in out
''',
)

# Operator instructions: compare baseline and candidate from the same local
# workstation/network, preferably with one authenticated tmux session per node.
readme_section = '''## SSH interaction latency benchmark

Use the read-only latency probe to measure the exact TUI build running on an
already-authenticated Edge Node. It never launches/cancels a Job or drops a
table. It measures screen-entry latency, Browser selection, log-search typing
when a recent Job is available, and SQL-path typing in New Job via a temporary
`/tmp` SQL file. Browser measurements issue the same read-only metadata queries
as opening Browse normally.

```powershell
python -m tools.prod_tui latency --config tools/prod_tui/config.yaml --samples 7 --json-report tools/prod_tui/reports/latency-main-node03.json
python -m tools.prod_tui latency --config tools/prod_tui/config-node04.yaml --samples 7 --json-report tools/prod_tui/reports/latency-candidate-node04.json --baseline-report tools/prod_tui/reports/latency-main-node03.json
```

For an A/B comparison, keep one node on the baseline SHA and one on the
candidate SHA, authenticate both tmux sessions, then run the two commands from
the same workstation/network. The report records the remote deployment SHA,
raw samples, median, p95, and local `capture-pane` overhead so network and
harness noise are visible. Comparing the same node before/after deployment is
also valid when both runs use the same terminal size and network path.

'''
replace(
    "tools/prod_tui/README.md",
    "## Level 1 and 2 Smoke Tests\n",
    readme_section + "## Level 1 and 2 Smoke Tests\n",
)

# Keep the repository's TUI skill facts synchronized with the new latency
# contract so future agents do not regress the SSH-tight paths.
replace(
    ".agents/skills/dispatch-textual-tui/references/COCKPIT-AND-PERFORMANCE.md",
    '''| Job Detail refresh: 1 second | `JobDetailScreen.on_mount` |
''',
    '''| Job Detail refresh: 1 second | `JobDetailScreen.on_mount` |
| Job Detail log-search repaint debounce: 150 ms | `LOG_SEARCH_DEBOUNCE_SECONDS` |
''',
)
replace(
    ".agents/skills/dispatch-textual-tui/references/COCKPIT-AND-PERFORMANCE.md",
    '''- Overview returns before listing jobs or reading logs while another screen is
  active. Job Detail currently keeps its one-second interval while mounted.
- Visible job IDs constrain View Logs and Cancel after filtering.
''',
    '''- Overview returns before listing jobs or reading logs while another screen is
  active. Job Detail currently keeps its one-second interval while mounted.
- Overview, History, Browse, and Job Detail paint their interactive shell before
  initial filesystem/Impala work completes; initial work runs through workers.
- Job Detail log-search keystrokes update the input immediately but coalesce
  full 200-line RichLog recoloring into one repaint after the typing burst.
- Visible job IDs constrain View Logs and Cancel after filtering.
''',
)

print("performance docs/test transformations applied")

---
name: dispatch-textual-tui
description: Build and review Dispatch Textual TUI changes with production-safe UI, performance, and mock-development discipline.
---

# Dispatch Textual TUI skill

Use this skill for any work touching the Dispatch terminal UI, including `dispatch/app.py`, `dispatch/screens/`, `dispatch/widgets/`, `dispatch/process.py`, `dispatch/runner.py`, job manifests, local mocks, UI styling, and interaction tests.

Dispatch is a server-side Textual TUI for launching and supervising Impala jobs from a Hadoop Edge Node. Users run `dispatch` from the directory containing SQL files. Jobs must survive terminal disconnects because the detached runner, not the TUI process, owns orchestrator execution.

## Read first

Before editing, read:

1. `README.md`
2. `AGENTS.md`
3. `docs/adr/` if present
4. `dispatch/app.py`
5. The relevant file in `dispatch/screens/` or `dispatch/widgets/`
6. `dispatch/process.py`, `dispatch/runner.py`, and `dispatch/models.py` before touching process, job, or manifest behavior
7. `mocks/dev-env.sh` and `mocks/scenarios/` before changing local development flows

Do not rely on old Windows GUI assumptions. The legacy PowerShell GUI is removed from the v1.0 product direction.

## Product invariants

Keep these invariants intact:

- The TUI is a supervisor and launcher, not the durable job owner.
- The launch-time current working directory is captured once and used for CSV destinations for the session.
- Job state is stored in manifests under the configured Dispatch data root.
- CSV outputs for CSV and Table + CSV jobs are plain, uncompressed files in the user's launch-time working directory.
- Table + CSV jobs are decomposed into table creation followed by a separate CSV export.
- The TUI must hard-refuse invalid source/destination combinations, missing Kerberos tickets, tickets with less than five minutes remaining, and more than two simultaneous Running jobs.
- `scr/` orchestrator changes are high risk. Do not change them unless the task explicitly requires it and the ADRs allow it.

## Textual architecture rules

Prefer this structure:

- `dispatch/app.py` owns the app shell, global bindings, theme/CSS registration, launch CWD capture, and startup routing.
- `dispatch/screens/*.py` own screen-level layout, bindings, and orchestration.
- `dispatch/widgets/*.py` own reusable visual components.
- Service/process logic stays outside widget code.
- Long-running work runs through async-safe helpers, Textual workers, or background-safe process abstractions. Never block the Textual event loop with `subprocess.run`, long file scans, sleep loops, network calls, or heavy computation inside event handlers.

Use Textual-native primitives first:

- `Screen`, `App`, `ComposeResult`, `Header`, `Footer`
- `DataTable`, `RichLog`, `Input`, `Button`, `Static`, `Label`, `Tree`, `MarkdownViewer`, `TabbedContent`, `ProgressBar` when appropriate
- `BINDINGS`, actions, and the command palette for keyboard-first workflows
- reactive state for UI state that affects rendering
- `set_interval` or workers for refresh loops, with cleanup on unmount

## Styling rules

The current app keeps CSS in `APP_CSS`. For small changes, preserve that pattern. For larger styling work, prefer moving CSS into a dedicated `.tcss` file in a separate PR rather than growing `app.py` indefinitely.

Style for a production SSH terminal:

- Do not optimize only for screenshots. It must remain readable over SSH, small terminals, and limited color themes.
- Preserve keyboard focus clarity.
- Keep status, errors, warnings, and running/progress states visually distinct.
- Avoid decorative elements that reduce density or legibility.
- Use Textual theme variables where possible; use hard-coded colors sparingly and consistently.
- Ensure narrow terminal fallbacks do not hide critical controls.

## UX rules

Dispatch should feel like a focused terminal IDE:

- Keyboard-first navigation with visible shortcuts in the footer or help text.
- All destructive or irreversible actions require an explicit confirmation path.
- Every job launch path must show what will run before execution: source, destination, output path/table, Kerberos status, queue/pool signals, and generated argv where relevant.
- Empty states should explain the next valid action.
- Errors must include the actionable cause, not only the raw exception.
- Logs should be tail-friendly, searchable where possible, and should not freeze while large.

Reference behavior to emulate:

- Toolong for large log viewing, tailing, searching, and compressed/log-scale performance.
- Harlequin for IDE-like panes, tables, keyboard flow, and terminal density.
- Elia for polished keyboard-first command/chat interactions.
- Frogmouth for navigation, history, bookmarks/back-stack style behavior.

## Performance rules

Performance is part of the UI contract.

- Do not re-read every manifest or full log file on every paint.
- Prefer incremental refreshes and cached parsed state with explicit invalidation.
- For log display, tail only the necessary window unless the user requests full history.
- Avoid rebuilding large `DataTable`s wholesale if only a few rows changed.
- Keep dashboard refresh work bounded and cancellable.
- Large file previews must cap bytes/lines and clearly show truncation.
- Avoid synchronous filesystem walks in event handlers.

## Mock-development rules

Local development must work without Hadoop, Kerberos, SMTP, or `/ads_storage/` by using mocks.

Recommended local flow:

```bash
source mocks/dev-env.sh
export DISPATCH_MOCK_SCENARIO=happy_path
python -m dispatch
```

Exercise at least these scenarios when touching launch, status, log, or error UI:

- `happy_path`
- `all_queues_full`
- `memory_exceeded`
- `syntax_error`
- `auth_error`
- `slow`

When behavior differs between mock and Edge Node, document it in the PR.

## Validation checklist

Before proposing a PR, run the strongest available subset:

```bash
python -m compileall dispatch scr
source mocks/dev-env.sh
DISPATCH_MOCK_SCENARIO=happy_path python -m dispatch
```

If automated Textual tests exist or are added, prefer:

```bash
pytest
```

For TUI behavior tests, use Textual's test pilot style rather than brittle terminal-output string comparisons. Validate at least:

- the app starts
- keyboard navigation works
- illegal launch combinations are refused
- mock scenarios surface clear status/errors
- running jobs do not block UI interaction
- narrow terminal layouts remain usable

## Review checklist

A Dispatch TUI PR is not ready if it:

- blocks the event loop
- changes job durability semantics
- writes CSVs outside the launch-time CWD without explicit product approval
- hides Kerberos or queue/pool failure reasons
- changes `scr/` casually
- adds UI polish without testing it in a mock scenario
- relies on local-only paths or corporate-only paths without fallbacks
- creates a screenshot-perfect layout that breaks over SSH or narrow terminals

## Output expectations for agents

When reporting work, include:

- files changed
- behavior changed
- validation run and result
- mock scenario used
- known gaps, especially anything requiring real Edge Node smoke testing

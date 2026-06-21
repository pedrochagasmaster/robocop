# AGENTS.md

## Codebase overview

This repository now targets **Dispatch v1.0**: a server-side Textual TUI for launching and supervising Impala jobs from a Hadoop Edge Node.

Users `ssh` to the Edge Node, `cd` to the directory containing their SQL files, run `dispatch`, and launch jobs that survive terminal disconnects.

| Component | Language | Runs on | Notes |
|---|---|---|---|
| `dispatch/` | Python 3.10+ + Textual | Edge Node / local mocks | Main TUI package and detached job supervision logic |
| `scr/Query_Impala_Parametrized.py` | Python 3.10+ stdlib | Edge Node | Existing production orchestrator for Impala table creation |
| `scr/download_to_csv.py` | Python 3.10+ stdlib | Edge Node | Existing production CSV export path |
| `scr/monthly_query_processor.py` | Python 3.10+ stdlib | Edge Node | Existing monthly/template processor |
| `mocks/` | Shell/Python fixtures | Local dev | Fake Hadoop/Kerberos/SMTP/Impala layer for offline development |
| `vendor/` | Wheels/artifacts | Edge Node install | Offline dependency source for the per-user venv, when present |

The legacy Windows GUI is not the product direction. Do not reintroduce Windows Forms or PowerShell GUI assumptions unless the user explicitly asks for legacy maintenance.

## Product invariants

- Jobs are described by on-disk manifests under the configured Dispatch data root.
- The TUI supervises jobs by reading manifests and logs; the detached runner owns orchestrator execution.
- The TUI captures the launch-time current working directory once.
- CSV outputs are written uncompressed to the launch-time working directory.
- Table + CSV jobs are decomposed into table creation followed by separate CSV export.
- The TUI must refuse illegal source/destination combinations, missing Kerberos tickets, tickets with less than five minutes remaining, and more than two simultaneously Running jobs.
- `scr/` orchestrators are production-sensitive. Prefer not to change them unless the task explicitly requires it and existing ADRs allow it.

## Dependency policy

`dispatch/` may use the pinned Textual dependency from `pyproject.toml`.

`scr/` scripts should remain standard-library-only unless the user explicitly approves a change to the production orchestrator dependency policy.

## Textual TUI skill

For any work touching `dispatch/app.py`, `dispatch/screens/`, `dispatch/widgets/`, UI styling, async/process behavior, mock scenarios, or TUI tests, use:

```text
.agents/skills/dispatch-textual-tui/SKILL.md
```

The skill contains the project-specific rules for Textual architecture, performance, SSH-terminal UX, mocks, and validation.

## Improve skill

For codebase audits, improvement opportunities, roadmap suggestions, or self-contained implementation plans for other agents to execute, use:

```text
.agents/skills/improve/SKILL.md
```

The skill is a read-only senior advisor: it surveys the codebase, produces prioritized findings, and writes handoff plans under `plans/` without modifying source code itself.

## Local development

Install and run locally with mocks:

```bash
source mocks/dev-env.sh
DISPATCH_EMAIL=you@example.com DISPATCH_PYTHON_BIN=$(command -v python3) ./install.sh
DISPATCH_MOCK_SCENARIO=happy_path python -m dispatch
```

Useful mock scenarios:

- `happy_path`
- `all_queues_full`
- `memory_exceeded`
- `syntax_error`
- `auth_error`
- `slow`

Captured emails are written to `mocks/sent_emails/` and should not be committed.

For the full development workflow, including local checks, Bitbucket sync,
edge-node update paths, and production harness validation, see
`docs/development-workflow.md`.

## Validation

Run the strongest available subset for the files touched.

Basic syntax/package validation:

```bash
python -m compileall dispatch scr
python -m dispatch --help
```

Mock smoke validation:

```bash
source mocks/dev-env.sh
DISPATCH_MOCK_SCENARIO=happy_path python -m dispatch
```

When changing launch, status, logs, or error presentation, also exercise the relevant failure mock scenario.

Before production merge, human reviewers may still need to smoke-test Textual over the corporate SSH chain, run `install.sh` against the real `/ads_storage/<user>/` mount, confirm Kerberos client output, compare M10 against production `impala-shell`, and deploy artifacts to `/ads_storage/dispatch/`.

## Issue tracker

Work is tracked in GitHub Issues for `pedrochagasmaster/robocop` using the `gh` CLI. See `docs/agents/issue-tracker.md` when present.

Canonical triage labels use the default label strings: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md` when present.

Keep both code remotes in mind: `origin` is GitHub for issues and occasional
VPN-bypass pushes, while `bitbucket` is the writable corporate remote and the
remote Edge Nodes can pull from. Do not push to either remote unless the user
explicitly asks.

## Domain docs

Use the single-context layout when present: optional root `CONTEXT.md` and `docs/adr/`. Read them before deep implementation work.

## Cursor Cloud specific instructions

Standard commands live in `README.md`, this file's `## Local development` / `## Validation` sections, and `pyproject.toml`. The notes below are the non-obvious cloud caveats.

- **Interpreter / venv.** Use the prebuilt virtualenv at `/workspace/.venv` (Python 3.12, satisfies `requires-python >=3.10`). The cloud update script creates it and installs runtime deps from the offline `vendor/` wheels plus `pytest`/`pytest-asyncio`. Run things as `/workspace/.venv/bin/python -m dispatch` / `... -m pytest`. The system `python3` has no `venv`/`pip` configured for project deps, so don't invoke `dispatch` outside this venv.
- **Always source `mocks/dev-env.sh` in the SAME shell before running the TUI or tests.** It puts the mock `klist`/`kinit`/`impala-shell` on `PATH`, starts the fake SMTP catcher, and exports `DISPATCH_*`. The detached job **runner inherits this environment from the TUI process**, so launching `python -m dispatch` without sourcing it first means launched jobs cannot find `impala-shell` and fail. The mock `klist` returns a valid ~8h Kerberos ticket by default, so no `kinit` is needed for happy-path launches.
- **Data root.** `dev-env.sh` sets `DISPATCH_DATA_ROOT=/tmp/ads_storage/$USER` (not `/ads_storage/...`). `install.sh` requires that dir to exist and be writable and needs `klist`/`impala-shell` on `PATH`, so only run it after sourcing `dev-env.sh` and with `DISPATCH_PYTHON_BIN=$(command -v python3)`. Run `install.sh` from the repo root; under `dash` it prints a harmless `Bad substitution` line but still completes.
- **Driving the TUI for tests.** It is a full-screen interactive Textual app; demo it through a desktop terminal + computer use. For deterministic, scripted launches the app honors `DISPATCH_TEST_PREFILL=<path-to-json>` to open the New Job form pre-filled. The full test suite (`/workspace/.venv/bin/python -m pytest`) takes ~50s.

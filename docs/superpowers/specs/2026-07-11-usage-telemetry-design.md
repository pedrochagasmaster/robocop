# Usage Telemetry Design

Date: 2026-07-11

## Problem

Operators need to know **who** is using Dispatch on the Edge Node and **how**
they use it (sessions, screens, Job launches, refusals). There is no existing
telemetry. Edge Nodes typically lack reliable egress to SaaS analytics, and
per-user `.dispatch` directories are mode `0700`, so private logs alone cannot
answer fleet-wide “who”.

## Goals

- Record enough product usage to answer: active users, session volume, which
  screens and Job `(Source, Destination)` combinations are used, and common
  launch refusals.
- Stay offline-first: append-only local files, no network calls, no new runtime
  dependencies.
- Never block or break the TUI / Job lifecycle if telemetry fails.
- Respect privacy: no SQL text, no absolute paths to user SQL, no email bodies.

## Non-goals

- Real-time dashboards or SaaS exporters.
- Changing Job / manifest / orchestrator semantics.
- Capturing keystroke-level interaction or SQL content.
- Cross-node aggregation beyond what shared `/ads_storage` already provides.

## Approaches considered

### A. Private JSONL only under `~/.dispatch/telemetry/`

Simple and private. Operators cannot see other users without elevated read
access across `/ads_storage/*/`. Weak for “who is using it” fleet-wide.

### B. Dual-write JSONL (private + shared rollup) + CLI summaries (chosen)

Emit the same events to:

1. `{dispatch_home}/telemetry/events.jsonl` (private, always when enabled)
2. `{shared_telemetry_dir}/users/{user}.jsonl` when that directory is writable

Default shared dir: `/ads_storage/dispatch/telemetry` (override with
`DISPATCH_TELEMETRY_DIR`). Operators run `dispatch telemetry summary` /
`dispatch telemetry who` against the shared dir (or a private dir for self).

### C. Third-party / HTTP analytics

Rejected: egress, corporate approval, dependency weight, and failure modes that
could hang the TUI.

## Architecture

```
TUI / jobs / screens
        │
        ▼
 dispatch.telemetry.emit(event, props)
        │
        ├─► private JSONL  (best-effort, flock)
        └─► shared JSONL   (best-effort, flock; skip if not writable)
```

`emit` is synchronous, bounded, and swallows all I/O errors after a debug log.
No workers, no background flush queue in v1 — event volume is low (sessions and
Job actions, not per-keystroke).

### Event schema

One JSON object per line:

| Field | Type | Notes |
|---|---|---|
| `ts` | ISO-8601 UTC `…Z` | Event time |
| `event` | string | See catalog below |
| `user` | string | `config.current_user()` |
| `session_id` | string | UUID for this TUI process |
| `version` | string | Dispatch version |
| `props` | object | Event-specific, may be empty |

### Event catalog

| Event | When | Props |
|---|---|---|
| `session_start` | App mount | `cwd_basename` only (not full path) |
| `session_end` | App unmount / quit | `duration_s` |
| `screen_view` | Top-level screen shown | `screen` ∈ `{overview,new_job,history,browser,help,job_detail}` |
| `job_launched` | Job created + runner start attempted | `job_id`, `source`, `destination` |
| `launch_refused` | Hard refuse before create | `reason` ∈ `{slot_cap,kerberos,validation}` |
| `job_cancelled` | User cancel from TUI | `job_id` |

### Identity (“who”)

- Primary: OS username already used for data roots.
- Do **not** log notification email addresses.
- Session id correlates events within one TUI run without inventing a durable
  device id.

### Opt-out / config

| Mechanism | Effect |
|---|---|
| `DISPATCH_TELEMETRY=0` (or `false`/`off`/`no`) | Disable all writes |
| `DISPATCH_TELEMETRY_DIR` | Override shared rollup root |
| Missing / unwritable shared dir | Private write still happens; shared skipped silently |

### CLI

Extend `__main__` with a subcommand group (default remains launching the TUI):

```text
dispatch                          # TUI (unchanged)
dispatch telemetry who [--days N] [--dir PATH]
dispatch telemetry summary [--days N] [--dir PATH] [--user NAME]
```

- `who`: distinct users, session counts, last seen, Job launch counts.
- `summary`: screen views and `(source, destination)` launch mix, refusal
  reasons. Defaults to shared dir when present, else the current user’s private
  telemetry file.

### Instrumentation seams

| Seam | Hook |
|---|---|
| Session | `DispatchApp.on_mount` / quit path |
| Screens | `DispatchApp` navigation helpers + Help action |
| Launch success | After successful `create_job_if_slot_available` in New Job flow |
| Launch refuse | New Job validation / slot / Kerberos refuse paths |
| Cancel | Cancel action that signals the runner |

### Testing seams

1. `telemetry.emit` / path helpers — file content and opt-out (unit).
2. `telemetry.summarize` / `who` — pure aggregation over fixtures (unit).
3. CLI argparse routing — `telemetry` subcommands do not start the TUI.
4. Light App mount test — `session_start` written when enabled under a temp
   data root (optional if expensive; prefer unit coverage of emit + hooks that
   call it).

## Failure and safety

- Telemetry must not raise into callers.
- Use `fcntl.flock` around appends (same pattern as launch locking).
- Cap props to known keys; never dump arbitrary form dicts.
- Do not change `scr/` or Job durability.

## Success criteria

- With telemetry enabled under a temp `DISPATCH_DATA_ROOT`, starting the app and
  launching a mock Job produces `session_start` and `job_launched` lines.
- `dispatch telemetry who` lists the user after those events.
- `DISPATCH_TELEMETRY=0` produces no files.
- Unwritable shared dir does not affect the TUI or private logging.
- `pytest` passes for new tests; no new package dependencies.

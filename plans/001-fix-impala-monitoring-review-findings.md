# Plan 001: Close all Impala-monitoring branch review findings

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the **STOP conditions** section occurs, stop and
> report; do not improvise. Keep monitoring read-only and observational: no
> failure in this feature may change an orchestrator return code, manifest
> state, email, pool choice, retry timing, CSV behavior, or cancellation path.
> When done, update this plan's row in `plans/README.md` unless a reviewer told
> you they maintain the index.
>
> **Drift check (run first)**:
>
> ```powershell
> git diff --stat 2c64341..HEAD -- docs/adr/0005-scr-modification-policy.md README.md dispatch/app.py dispatch/app.tcss dispatch/config.py dispatch/impala_monitor.py dispatch/impala_monitor_http.py dispatch/monitor_service.py dispatch/runner.py dispatch/screens/dashboard.py dispatch/screens/job_detail.py scr/_common.py tests/test_impala_monitor_http.py tests/test_monitor_service.py tests/test_runner_integration.py tests/test_scr_common.py tests/test_ui_snapshots.py
> ```
>
> If any in-scope file changed since this plan was written, compare the
> **Current state** excerpts with live code before proceeding. A semantic
> mismatch is a STOP condition.

## Status

- **Priority**: P1
- **Effort**: L (multi-day; protocol, service, transport, TUI, and tests)
- **Risk**: HIGH — this crosses production-sensitive `scr/`, background
  scheduling, HTTP transport, and an SSH-constrained Textual screen
- **Depends on**: none
- **Category**: bug, security, performance, tests, architecture, docs
- **Planned at**: commit `2c64341`, 2026-07-17

## Why this matters

The branch has the right overall architecture, but the implementation loses
the original observation when a transparent retry arrives, cannot distinguish
pool fallback from a separate Table+Csv orchestrator call, and leaves the
implemented recovery path unreachable. It also accepts non-anchored monitor
lines, does not honor the configured connect timeout, rereads every sidecar in
full forever, can leak a foreground subscription across screen unmount, and
breaks the supported 80x24 layout.

The fixes are coupled. Correct retry wording requires a durable hierarchy of
job -> orchestrator call -> shell execution -> query -> transparent retry.
That hierarchy must be established in the event protocol before the service,
recovery path, and UI can be repaired without guessing.

## Review-finding coverage

| Review finding | Fix location | Primary verification |
|---|---|---|
| ADR-0005 does not explicitly authorize this observability seam | Step 1 | ADR diff and Maintainer review |
| Monitor-line parsing is not strictly anchored | Step 2 | adversarial `test_scr_common.py` cases |
| Orchestrator-call/pool-fallback lineage is absent | Steps 2–3 | runner + replay hierarchy tests |
| Transparent retry erases the parent's observation/final poll | Step 4 | append-retry-after-poll regression test |
| Raw identity tuples form a data clump | Step 4 | typed poller-key assertions/typecheck |
| Sidecars are reread in full on every tick | Step 4 | read-count/byte-count cache tests |
| Recovery discovery has no production caller | Steps 5–6 | fake-client + Pilot recovery tests |
| Background monitoring starts only after Job Detail | Step 5 | Dashboard/service integration test |
| Connect/read timeout values are collapsed | Step 5 | connection/socket timeout tests |
| Async subscription can paint/leak after unmount | Step 6 | blocking fake-service lifecycle test |
| 80x24 monitor panel overlaps/off-screen content | Step 6 | region assertions at three terminal sizes |

## Current state

### Repository and verification contract

- Python 3.10+ package with Textual `8.2.5`; development dependencies live in
  `.venv` on this Windows host.
- `AGENTS.md` requires current GitHub `main`, a short-lived branch, full pytest,
  and a GitHub PR. Do not merge, deploy, tag, push Bitbucket, or alter branch
  protection.
- `scr/` must remain standard-library-only. ADR-0005 also freezes public CLI,
  email, queue order, retry timing, and the `_common.py` API as a
  production-sensitive contract.
- The monitoring contract requires exact identity capture, read-only polling,
  manifest authority, foreground/background cadence of 2s/30s, refusal on
  ambiguous recovery, and an explicit five-level attempt hierarchy.
- `dispatch-textual-tui` rules require blocking I/O off the event loop, stale
  result guards, bounded reads/repaints, keyboard access, and a usable 80x24
  layout.

### Governance mismatch

`docs/adr/0005-scr-modification-policy.md:9-33` lists allowed `scr/` changes as
obvious bug fixes, three named factorisations, environment externalisation, and
dead-code removal. The branch introduces a new subprocess observability seam.
The research contract calls it a narrow improvement under ADR-0005, but the ADR
does not say so. Resolve that documentation conflict before treating the code
as approved.

### Event protocol and hierarchy gap

`dispatch/runner.py:145-152` currently creates one environment and reuses it
for every call:

```python
orchestrator_env = _orchestrator_env(manifest, job_dir)
for call in manifest["orchestrator_calls"]:
    with subprocess.Popen(
        call["argv"], stdout=log, stderr=log, env=orchestrator_env
    ) as proc:
        ...
```

`scr/_common.py:118-127` emits job, shell, pool, timestamp, and sequence, but no
orchestrator-call identity or shell relation:

```python
payload = {
    "v": 1,
    "type": event_type,
    "job_id": self._job_id,
    "shell_execution_id": self._shell_execution_id,
    "seq": self._seq,
    "pool": self._pool,
    "ts": ...,
    **extra,
}
```

`dispatch/monitor_service.py:107-118` therefore attaches shells directly to a
job snapshot. It cannot tell whether a later shell is a pool fallback inside
the same call or the next Table+Csv call.

### Unanchored identity extraction

`scr/_common.py:44-45,182-191` uses unanchored patterns with `.search()`:

```python
MONITOR_LINE_RE_V1 = re.compile(r"Query state can be monitored at:\s*(\S+)")
RETRIED_LINE_RE_V1 = re.compile(r"Retried query link:\s*(\S+)")
...
monitor_match = MONITOR_LINE_RE_V1.search(text)
```

The local URL validator extracts a host and `query_id` but does not require the
captured path to be the exact `/query_plan?query_id=<hex16:hex16>` shape.

### Observation loss on retry

`dispatch/monitor_service.py:322-333` returns only live leaves, and
`_refresh_job_locked` at lines 602-621 preserves observations only for those
leaves. Once `query_retried` makes the original query a parent, its last
observation disappears. The original poller is then deleted at lines 662-676,
so it never receives the required final confirmation poll.

### Repeated full-file replay

Every `_tick()` calls `_refresh_job_locked()` for every retained job
(`dispatch/monitor_service.py:698-705`). That method calls
`Path.read_bytes()` through `replay_event_file()` even when size/mtime did not
change. `unsubscribe()` intentionally retains job state, so every job ever
viewed continues this work at 30-second cadence.

Follow the existing invalidation pattern in
`dispatch/screens/dashboard.py:204-228`: stat first, reuse cached data when
size and mtime match, and bound the bytes read.

### Discovery and background integration gaps

- `ImpalaMonitorClient` implements `discover_coordinators()` and `discover()`,
  but `MonitorService.MonitorClient` exposes only `observe()` and no production
  UI calls discovery.
- `MonitorService.register_job()` exists, but production never calls it.
  `DispatchApp` starts an empty service; only Job Detail subscribes.
- `DashboardScreen._refresh_jobs_async()` already obtains the active manifest
  list off-thread and is the correct production seam for synchronizing
  background registrations without another filesystem walk.

### Timeout mismatch

`dispatch/impala_monitor_http.py:211-222` receives `(3s, 10s)` but passes their
maximum to `urllib`:

```python
connect_timeout, read_timeout = timeout
socket_timeout = max(connect_timeout, read_timeout)
response = opener.open(request, timeout=socket_timeout)
```

This permits a 10-second connection attempt instead of the required 3-second
connect bound. The implementation must remain stdlib and retain verified TLS
and no-redirect behavior.

### Screen lifecycle and responsive layout gaps

`dispatch/screens/job_detail.py:217-219` applies a threaded subscription result
without checking that the screen is still current. `_monitor_subscribed` is set
only after the await, so unmount can miss the matching unsubscribe.

At 80x24, the current CSS produces these measured regions:

```text
#monitor-panel  Region(x=6, y=13, width=73, height=5)
.action-bar     Region(x=5, y=17, width=75, height=6)
#log-panel      Region(x=6, y=21, width=73, height=8)
```

The log overlaps the action bar and extends beyond the viewport. Existing new
monitor-panel tests use only 120x40.

## Commands you will need

Run from `D:\Projects\robocop` in PowerShell:

| Purpose | Command | Expected on success |
|---|---|---|
| Branch state | `git status --short --branch` | expected feature/fix branch; no unexpected tracked edits |
| Focused protocol | `.\.venv\Scripts\python.exe -m pytest tests/test_scr_common.py tests/test_runner_integration.py -q` | all pass |
| Focused service | `.\.venv\Scripts\python.exe -m pytest tests/test_monitor_service.py -q` | all pass |
| Focused HTTP | `.\.venv\Scripts\python.exe -m pytest tests/test_impala_monitor_http.py -q` | all pass |
| Focused TUI | `.\.venv\Scripts\python.exe -m pytest tests/test_ui_snapshots.py tests/test_cockpit.py -q` | all pass |
| Compile | `.\.venv\Scripts\python.exe -m compileall -q dispatch scr` | exit 0 |
| Lint | `.\.venv\Scripts\ruff.exe check dispatch tests; .\.venv\Scripts\ruff.exe check scr` | both exit 0 |
| Format | `.\.venv\Scripts\ruff.exe format --check dispatch tests` | exit 0; no files reformatted |
| Typecheck | `.\.venv\Scripts\mypy.exe dispatch/sql.py dispatch/jobs.py dispatch/manifest.py` | exit 0, no errors |
| CLI smoke | `.\.venv\Scripts\python.exe -m dispatch --help` | exit 0; help printed |
| Full suite | `.\.venv\Scripts\python.exe -m pytest -n 4 --dist loadfile` | all pass; baseline is 854 passed, 25 skipped at `2c64341` |
| Diff hygiene | `git diff --check` | no output, exit 0 |

Do not run formatters in write mode until the implementation is complete and
the diff has been reviewed for scope.

## Suggested executor toolkit

- Read and follow `.agents/skills/dispatch-textual-tui/SKILL.md` before Steps 5
  and 6; its module map, lifecycle, performance, and validation references are
  mandatory for this screen.
- Use the `tdd` skill if available: add each regression test first, prove that
  it fails for the intended reason, then implement the smallest satisfying
  change.
- Re-read `docs/adr/0005-scr-modification-policy.md` before touching `scr/`.

## Scope

**In scope — the only files permitted to change:**

- `docs/adr/0005-scr-modification-policy.md`
- `README.md` — document only the optional recovery seed configuration
- `dispatch/app.py`
- `dispatch/app.tcss`
- `dispatch/config.py`
- `dispatch/impala_monitor.py`
- `dispatch/impala_monitor_http.py`
- `dispatch/monitor_service.py`
- `dispatch/runner.py`
- `dispatch/screens/dashboard.py`
- `dispatch/screens/job_detail.py`
- `scr/_common.py`
- `tests/test_impala_monitor_http.py`
- `tests/test_monitor_service.py`
- `tests/test_runner_integration.py`
- `tests/test_scr_common.py`
- `tests/test_ui_snapshots.py`
- `tests/test_cockpit.py` only if needed for Dashboard background registration
- `plans/README.md` status update only

**Out of scope — do not touch even if related:**

- `dispatch/manifest.py` schema or manifest job states
- public argv/flags or exit codes of any `scr/` script
- queue order, retry count/timing, email subjects/bodies, CSV temp/replace logic
- cancellation behavior or any Impala write/cancel endpoint
- `docs/monitoring/` and `docs/research/` (currently untracked and potentially
  sensitive; preserve them exactly)
- dependency additions; the fix must remain stdlib plus existing project deps
- deployment, Bitbucket, release tags, branch protection, or merge operations
- production/Edge Node probes without explicit Release Operator instruction

If a correct fix requires an out-of-scope file, stop and report which invariant
forces the expansion.

## Git workflow

- Continue from `feat/impala-monitoring` at `2c64341`, or create
  `codex/fix-impala-monitoring-review` directly from that commit. Do not branch
  from `main`; these fixes depend on the feature commits under review.
- Before switching branches, run `git status --short --branch`. Preserve the
  existing untracked `.tmp-pytest/`, `docs/monitoring/`, and `docs/research/`.
- Make focused commits in this order:
  1. `Document scr monitoring policy`
  2. `[scr/] Harden monitoring identity events`
  3. `Model orchestrator monitoring lineage`
  4. `Preserve and incrementally poll monitor attempts`
  5. `Wire monitor recovery and background registration`
  6. `Fix Job Detail monitoring lifecycle and layout`
- Do not push or open a PR unless the operator explicitly requests it. If a PR
  is later requested, its `[scr/]` section must include what changed, why it is
  safe, regression risk, all-scenario evidence, and byte-equivalence evidence.

## Steps

### Step 1: Reconcile ADR-0005 before changing the production seam

Amend `docs/adr/0005-scr-modification-policy.md` with a narrowly worded allowed
category for read-only execution observability at the `impala-shell` seam. The
new rule must require all of the following:

- no public CLI, queue, retry, email, CSV, or exit-code behavior change;
- stdout/stderr byte preservation for downstream classification;
- concurrent draining so neither child pipe can deadlock;
- only strict, versioned, allowlisted event shapes with no SQL/error bodies;
- bounded sidecar writes that can fail closed without affecting execution;
- every mock scenario and pre/post equivalence evidence under the existing
  two-reviewer process.

Do not weaken any existing prohibition. This amendment resolves documentation
drift; it is not permission for generic future `scr/` instrumentation.

**Verify**:

```powershell
git diff --check -- docs/adr/0005-scr-modification-policy.md
rg -n "observability|stdout|stderr|sidecar|CLI|queue|retry" docs/adr/0005-scr-modification-policy.md
```

Expected: exit 0; the new category and safeguards are present, and the existing
prohibitions remain.

### Step 2: Harden identity parsing and emit a versioned orchestrator-call protocol

#### 2a. Make monitor-line recognition exact

In `scr/_common.py`:

1. Replace `.search()` with `fullmatch()` over one decoded stderr line after
   removing only its trailing `\r\n`.
2. Keep the two exact prefixes separately versioned.
3. Parse the captured URL once into a small private value object or named tuple
   containing `coordinator_base_url` and `query_id`.
4. Accept only:
   - `http` or `https`;
   - no userinfo or fragment;
   - a present syntactically valid host and valid port;
   - path exactly `/query_plan`;
   - exactly one query parameter, `query_id`;
   - query ID matching lowercase `hex16:hex16`.
5. Return no event for leading text, trailing text, alternate endpoints,
   duplicate/extra query parameters, credentials, invalid ports, malformed IDs,
   or any line that merely contains the prefix inside an error/SQL message.
6. Preserve every stderr byte exactly as read regardless of match outcome.

Do not add network access or import `dispatch` into `scr/`.

#### 2b. Add event protocol v2 with durable call lineage

In `dispatch/runner.py`, build a fresh environment per manifest call. Add:

- `DISPATCH_ORCHESTRATOR_CALL_ID=call-0001`, `call-0002`, ... derived
  deterministically from list position;
- `DISPATCH_ORCHESTRATOR_CALL_INDEX` using the same one-based position;
- `DISPATCH_ORCHESTRATOR_SCRIPT` using the existing `call["script"]` value.

The subprocess argv and manifest remain unchanged.

In `scr/_common.py`, emit `v: 2` events containing the call ID/index/script and
shell relation. Within one orchestrator process, number `run_impala_shell()`
invocations monotonically:

- first shell: `shell_relation="initial"`;
- later shell: `shell_relation="orchestrator_pool_fallback"`.

Use a private, lock-protected counter. Do not change the public
`run_impala_shell(argv, *, pool)` signature. Continue emitting the same four
event types and query relation semantics. Monitoring remains a no-op when the
event path is unset.

Keep v1 replay compatibility in the service later: old v1 shells must be
represented truthfully as legacy/unknown call lineage, never guessed to be a
pool fallback.

#### 2c. Tests

Add tests before implementation for:

- leading/trailing injected text is rejected;
- `/cancel_query`, `/query_stmt`, extra params, duplicate `query_id`, userinfo,
  fragment, invalid port, and invalid ID are rejected;
- CRLF and LF exact valid lines are accepted;
- returned stdout/stderr bytes remain identical for accepted and rejected
  lines;
- two calls in one runner manifest receive deterministic distinct call IDs;
- two shell launches in one orchestrator process emit initial then
  pool-fallback relations;
- event-path-unset and unwritable-path behavior remain harmless;
- existing mock scenarios retain exit/output/email/CSV behavior.

**Verify**:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scr_common.py tests/test_runner_integration.py -q
.\.venv\Scripts\ruff.exe check scr
```

Expected: all tests pass; Ruff exits 0.

### Step 3: Represent the complete hierarchy without inferred relationships

Refactor `dispatch/monitor_service.py` so immutable snapshots explicitly model:

```text
MonitorSnapshot
  OrchestratorCallAttempt(call_id, index, script)
    ShellExecutionAttempt(shell_relation, pool, timestamps, returncode)
      QueryAttempt(relation="initial")
        QueryAttempt(relation="transparent_retry")
```

Requirements:

1. Introduce a frozen `OrchestratorCallAttempt` and matching mutable builder
   node. `MonitorSnapshot` owns `orchestrator_calls`, not a flat shell tuple.
2. Add a typed `ShellRelation` literal with only `initial`,
   `orchestrator_pool_fallback`, and `unknown_legacy`.
3. Parse v2 events into calls by deterministic call ID; validate consistent
   index/script metadata across that call and skip conflicting events rather
   than merging them.
4. Continue accepting v1. Put each legacy shell in an isolated deterministic
   legacy call and mark its relation `unknown_legacy`; never infer fallback.
5. Keep multiple `query_discovered` events within one shell as sibling
   statements. Only `query_retried` creates a transparent-retry child.
6. Provide explicit traversal helpers:
   - all calls in event order;
   - all shells in a call;
   - all query nodes recursively, including superseded parents;
   - current pollable leaves.
7. Update tests and UI fixtures to construct the full hierarchy. Do not retain
   a flat compatibility property that lets production code ignore call
   lineage; migrate every caller in scope.

Add replay tests for:

- two shells in one call (initial plus pool fallback);
- two separate Table+Csv calls, proving the second call is not a fallback;
- multi-statement siblings within one shell;
- transparent retry below the correct query;
- mixed v1/v2 or malformed metadata handled without invented lineage;
- restart replay produces the same hierarchy.

**Verify**:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_monitor_service.py tests/test_ui_snapshots.py -q
```

Expected: all hierarchy and migrated fixture tests pass.

### Step 4: Preserve every observation and tail sidecars incrementally

#### 4a. Preserve superseded attempts and perform one final poll

Replace raw `(job_id, coordinator_base_url, query_id)` tuple keys with a frozen
private `_PollerKey` dataclass. This closes the Data Clump while preventing
field-order mistakes.

During replay/synchronization:

- collect and restore observations for **all query nodes**, not only leaves;
- poll current leaves normally;
- when a previously live query becomes superseded by a transparent retry,
  keep its poller for exactly one immediate final confirmation poll;
- after that poll, stop/remove the poller but retain its last observation in
  the immutable history;
- if that final poll reports disappearance/unavailability, retain the last
  good phase and attach the availability error, then stop because the durable
  retry event already proves the identity is superseded;
- never restart a stopped terminal/superseded poller during later refreshes.

The visible history must preserve the original `EXCEPTION`, `RETRYING`, or
`RETRIED` observation after the retry child appears.

Regression test sequence:

1. Write shell + initial query events.
2. Poll the initial query as `EXCEPTION`.
3. Append `query_retried` in a later refresh.
4. Assert the parent still has its observation.
5. Assert one final parent poll and one live retry poll occur.
6. Assert no further parent polls occur.

#### 4b. Replace full replay with a bounded tail cursor

Add per-job tail state containing at least file identity, byte offset, size,
mtime, pending partial UTF-8 bytes, and the persistent hierarchy builder.

- Missing file: unavailable, without repeated open attempts beyond the stat.
- First appearance: read from offset zero, bounded by the writer's 1 MiB cap.
- Unchanged size+mtime: do not open or parse the file and do not increment
  snapshot generation.
- Growth: seek to the prior byte offset and read only appended bytes.
- Partial final line: retain bytes until a newline arrives; do not parse it.
- Truncation/replacement: reset and replay once from zero; reattach all
  observations by validated identity.
- Unreadable file: retain the last snapshot and surface monitoring unavailable;
  do not discard history.
- Drop job/tail state only when it is no longer background-registered, has no
  subscribers, and has no unfinished/final-confirmation poller.

Add tests that count `open`/read calls and bytes:

- ten unchanged ticks perform zero additional body reads/parses;
- one appended event reads only the appended suffix;
- a partial line is parsed once after completion;
- truncation performs one bounded replay;
- stopped/untracked jobs are eventually pruned.

**Verify**:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_monitor_service.py -q
```

Expected: all tests pass, including exact poll counts and read counts.

### Step 5: Enforce transport timeouts and make monitoring active/recoverable

#### 5a. Enforce distinct connect and read timeouts while retaining urllib

Keep the injected `Transport` protocol and stdlib `urllib.request` design. Do
not add `requests`, disable TLS verification, or follow redirects.

Implement a small custom urllib HTTP/HTTPS handler backed by an
`http.client.HTTPConnection`/`HTTPSConnection` subclass:

1. `opener.open(..., timeout=connect_timeout)` applies only to connection/TLS
   establishment.
2. In the connection subclass, after `connect()` succeeds, call
   `self.sock.settimeout(read_timeout)` before response headers/body are read.
3. Continue using `ssl.create_default_context(cafile=...)` for HTTPS.
4. Preserve raw 3xx responses with no redirect following.
5. Keep `_read_capped()` and the body cap unchanged.
6. Ensure both HTTP dev mode and HTTPS production mode have the same timeout
   semantics.

Avoid reaching through undocumented `response.fp.raw._sock` attributes; the
connection subclass must own the socket transition through stable
`http.client` APIs.

Update fake and thin integration tests to assert:

- connection constructor/open receives exactly 3 seconds;
- socket timeout switches to exactly 10 seconds after connect;
- connect timeout and read timeout exceptions both degrade to monitoring
  unavailable;
- TLS context, CA bundle, redirect refusal, retries, and breaker behavior stay
  green.

#### 5b. Synchronize background monitoring from the Dashboard's existing read

Add `MonitorService.sync_background_jobs()` accepting the currently active
`(job_id, job_dir)` set. Track background registration separately from
foreground subscriber count.

In `DashboardScreen._refresh_jobs_async()`, after `jobs.active_jobs()` returns,
call this service method off the event loop using the same already-loaded
manifest list. Register only `Pending` and `Running` jobs. Do not start another
manifest scan. Jobs no longer active should lose background registration but
remain until final-confirmation pollers stop or foreground subscribers leave.

Tests must prove:

- a Running job is registered on the first Dashboard refresh without opening
  Job Detail;
- it polls at 30-second cadence;
- opening Job Detail tightens the same poller to 2 seconds without duplication;
- closing Job Detail returns it to 30 seconds;
- terminal/unlisted jobs are pruned after final confirmation;
- Dashboard filter/cursor input still causes no filesystem walk.

#### 5c. Wire refusal-first operator recovery

Move `DiscoveryCriteria` to `dispatch/impala_monitor.py` so both the HTTP client
and service can depend on the pure domain type without making
`monitor_service.py` import urllib/ssl.

Extend the service-side `MonitorClient` protocol with
`discover_coordinators()` and `discover()`. Add a synchronous
`MonitorService.recover_identity(...)` operation that:

- requires an explicit operator call; never runs from `_tick()`;
- seeds coordinator discovery from a validated captured coordinator or the
  optional configured `DISPATCH_IMPALA_MONITOR_SEED_URL`;
- accepts a fully formed `DiscoveryCriteria` and delegates matching to the
  existing client;
- attaches a unique result in memory to exactly one shell lacking a query;
- refuses zero, multiple, conflicting, or context-free matches with a typed,
  sanitized error;
- never writes the event file or manifest and never logs SQL/full response
  bodies;
- survives ordinary refreshes in the service's in-memory recovered-identity
  map; after app restart, the operator must recover again.

Add `config.impala_monitor_seed_url()` and document the optional environment
variable in `README.md`. It is an operator-provided validated base URL, not a
host allowlist bypass. HTTPS remains the default; HTTP still requires the
existing dev/mock flag.

Build recovery criteria only from exact stored job/call data:

- user from `manifest["user"]`;
- bounded start window around `manifest["started_at"]`;
- statement prefix, statement type, and expected database derived from the
  specific orchestrator call's existing argv/job SQL, using existing SQL
  parsing helpers rather than heuristic string slicing;
- call ID/index from the v2 hierarchy.

Support only cells/calls where all criteria are derivable without guessing.
For monthly multi-statement, missing start time, unknown database/type, legacy
v1 lineage, or more than one candidate shell, return a clear
`identity unavailable/ambiguous` result. Never weaken criteria to force a
match.

**STOP within this substep** if `statement_type` or expected database cannot be
derived from stored call data for at least one single-statement SqlFile job
without changing the manifest schema. Report the missing datum instead of
adding permissive discovery or modifying `dispatch/manifest.py`.

**Verify**:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_impala_monitor_http.py tests/test_monitor_service.py tests/test_cockpit.py -q
```

Expected: all timeout, recovery, background-registration, and existing tests
pass.

### Step 6: Make Job Detail lifecycle-safe, lineage-aware, and responsive

#### 6a. Guard every threaded monitor result

In `JobDetailScreen`:

- capture a mount/generation token before each `asyncio.to_thread()` call;
- after await, verify `self.is_mounted`, `self.app.screen is self`, and token
  equality before painting;
- if `subscribe()` completed after the screen became stale, immediately pair
  it with `unsubscribe()` off-thread and do not set `_monitor_subscribed`;
- catch service/recovery exceptions and show a concise monitoring-only error;
- store and stop/pause the monitor refresh timer on unmount;
- keep `_monitor_refresh_in_flight` correct in success, stale, cancellation,
  and failure paths;
- never let monitoring affect log tail, cancel, clone, or manifest rendering.

Add a blocking fake service test: start mount, pop the screen before subscribe
returns, release the fake, and assert exactly one subscribe plus one compensating
unsubscribe, no widget paint, and no retained foreground subscriber.

#### 6b. Render pool fallback using call lineage

Update current-attempt and history traversal for the new hierarchy:

- transparent retry: parent followed by child can say
  `attempt failed; job retrying`;
- pool fallback: a failed query in one shell followed by a shell explicitly
  marked `orchestrator_pool_fallback` in the **same call** gets the same wording;
- next Table+Csv orchestrator call is not called a retry;
- later statement in one multi-statement shell is not called a retry;
- legacy/unknown lineage never invents retry wording;
- manifest state remains visually separate and authoritative.

Add an `M` keyboard binding/action for recovery. Show it only when recovery is
both needed and safely possible. Run `recover_identity()` in a thread, guard
the result with the same screen token, notify on refusal/ambiguity, and never
add a generic URL input or network action.

#### 6c. Restore 80x24 without hiding primary interaction

Add a screen-local `_update_layout_mode()` called from `on_mount` and
`on_resize`, following `DashboardScreen._update_layout_mode()`.

For heights below 30:

- hide the multi-line attempt history first;
- apply a compact class to the monitor panel/current attempt;
- reduce secondary margins/borders/min-heights as needed;
- keep manifest state, one-line current Impala attempt, bounded log content,
  Back, Cancel when applicable, and Footer keyboard help visible;
- do not rely on color alone.

At 120x40 and wider, restore the full history. Resizing in both directions must
preserve follow mode, focus, subscription count, and current snapshot.

Add Pilot region assertions at `(80, 24)`, `(120, 40)`, and a wider size:

- every visible panel ends before the action bar starts;
- action bar ends before the Footer and within viewport height;
- log panel has positive usable height;
- Back and applicable Cancel remain visible and keyboard reachable;
- compact mode shows the current attempt but not history;
- normal/wide mode restores history;
- resize does not create another service subscription or lose focus/selection.

Exercise queued, running/progress, transparent retry, pool fallback, recovery
refusal, and monitoring-unavailable states. If practical, use the existing mock
environment for one manual TUI walkthrough at all three sizes; do not commit
captures or temporary data.

**Verify**:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ui_snapshots.py tests/test_cockpit.py -q
```

Expected: all tests pass, including exact 80x24 non-overlap and stale-unmount
regressions.

### Step 7: Run ADR-0005 equivalence and the complete repository gates

Before declaring the plan done:

1. Run the complete mock scenario contract and all `scr/`-adjacent tests.
2. Compare pre-change and post-change stdout, stderr, exit code, email capture,
   CSV result/temp handling, pool order, and retry behavior for every scenario.
   Event-sidecar differences are expected; execution outputs are not.
3. Confirm no product path constructs or fetches `cancel_query`, no TLS
   verification disablement exists, and `scr/` imports only stdlib modules.
4. Run every CI-equivalent command and the full parallel suite.
5. Inspect `git status --short` and ensure only in-scope files plus the user's
   pre-existing untracked directories are present.

**Verify**:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_mock_contract.py tests/test_scr_common.py tests/test_runner_integration.py tests/test_download_to_csv_atomic.py tests/test_monthly_query_processor.py -q
.\.venv\Scripts\python.exe -m compileall -q dispatch scr
.\.venv\Scripts\ruff.exe check dispatch tests
.\.venv\Scripts\ruff.exe check scr
.\.venv\Scripts\ruff.exe format --check dispatch tests
.\.venv\Scripts\mypy.exe dispatch/sql.py dispatch/jobs.py dispatch/manifest.py
.\.venv\Scripts\python.exe -m dispatch --help
.\.venv\Scripts\python.exe -m pytest -n 4 --dist loadfile
git diff --check
git status --short --branch
```

Expected: every command exits 0; full suite has no failures; no unexpected
tracked or generated files appear.

## Test plan

### `tests/test_scr_common.py`

- Exact full-line acceptance for monitor and retry link, LF and CRLF.
- Rejection of prefix embedded in other text, suffix text, alternate endpoint,
  extra/duplicate params, credentials, fragments, invalid port/ID.
- Byte-equivalent stdout/stderr and harmless write failures.
- v2 call metadata and initial/fallback shell relation.

Use the current deadlock, byte-equivalence, malformed URL, and unwritable-path
tests as the structural pattern.

### `tests/test_runner_integration.py`

- Fresh per-call environment with deterministic call ID/index/script.
- Two-call Table+Csv manifest produces two call identities without changing
  argv or output behavior.
- Existing pool pinning and monitoring env tests remain green.

### `tests/test_monitor_service.py`

- v2 explicit call/shell/query hierarchy plus truthful v1 compatibility.
- Transparent-retry parent observation survives later retry append.
- Exactly one superseded-parent confirmation poll.
- Pool fallback and separate orchestrator calls remain distinct.
- `_PollerKey` use and one poller per live identity.
- Incremental read/no-op unchanged ticks/partial line/truncation/pruning.
- 30s background sync before any Job Detail visit; 2s foreground transition.
- Unique recovery attachment and zero/multiple/unsupported refusal paths.

Use existing fake clock/client and event-line helpers; extend them rather than
introducing real sleeps/network.

### `tests/test_impala_monitor_http.py`

- Exact connect timeout and post-connect socket read timeout.
- TLS/CA/no-redirect/body cap/breaker regressions.
- Discovery remains unique-or-refuse and sanitizes errors.

### `tests/test_ui_snapshots.py` and `tests/test_cockpit.py`

- Real service-shaped hierarchy, not a flat fake snapshot.
- Correct transparent retry and pool fallback wording; no false retry across
  Table+Csv calls or statement siblings.
- Operator recovery success/refusal and stale result guard.
- Blocking subscribe/unmount compensation.
- 80x24, 120x40, and wide region assertions plus resize behavior.
- Dashboard registers Running/Pending jobs without a second filesystem scan.

## Done criteria

All boxes must be machine-checkably true:

- [ ] ADR-0005 explicitly and narrowly covers the monitoring seam without
      weakening public behavior safeguards.
- [ ] `scr/_common.py` accepts only exact monitor/retry lines and exact
      read-only query-plan URL shapes.
- [ ] New events carry deterministic orchestrator-call identity and explicit
      initial/pool-fallback shell relation; v1 remains truthfully readable.
- [ ] Snapshots model job -> call -> shell -> query -> transparent retry.
- [ ] A transparent-retry append preserves the parent observation and performs
      exactly one final parent poll.
- [ ] Unchanged sidecars cause zero repeated body reads/parses.
- [ ] `_PollerKey` replaces raw three-string tuple keys in service code.
- [ ] Running/Pending jobs receive 30s background monitoring before Job Detail
      is opened, with one shared poller tightening to 2s while subscribed.
- [ ] Recovery has a keyboard-accessible production path and refuses any case
      whose exact criteria cannot be derived uniquely.
- [ ] Connection establishment is bounded by 3s and socket reads by 10s.
- [ ] Unmount during threaded subscribe causes a compensating unsubscribe and
      no stale paint.
- [ ] 80x24 region assertions prove no overlap/off-screen primary controls;
      120x40 and wide layouts retain full history.
- [ ] No manifest, CLI, email, queue, retry, CSV, cancel, or TLS-verification
      contract changed.
- [ ] Focused tests, all mock scenarios, compile, Ruff, format check, targeted
      mypy, CLI smoke, full pytest, and `git diff --check` all exit 0.
- [ ] `git status --short` shows no generated artifacts or out-of-scope tracked
      edits.
- [ ] `plans/README.md` marks Plan 001 `DONE` only after all gates pass.

## STOP conditions

Stop and report; do not improvise if any condition occurs:

- Live in-scope code no longer matches the semantic current state described
  above after the drift check.
- The ADR amendment would require weakening an existing CLI/email/queue/retry
  prohibition or cannot obtain Maintainer acceptance in review.
- Production shell output differs from the exact documented line variants;
  preserve isolated versioned constants and request real sanitized evidence.
- A correct call/fallback relationship would require changing manifest schema
  or public orchestrator argv.
- Recovery user/time/prefix/type/database criteria cannot be derived safely for
  at least one single-statement SqlFile job from current stored call data.
- Separate timeouts cannot be implemented through stable stdlib
  `urllib`/`http.client` APIs on both Python 3.10 and 3.12.
- 80x24 can only be made non-overlapping by hiding manifest state, all current
  Impala status, logs, Back, or applicable Cancel.
- Any focused verification fails twice after a reasonable correction.
- Tests reveal a change to stdout/stderr bytes, exit codes, email, pool order,
  retry timing, or CSV behavior outside the event sidecar.
- The fix requires touching an out-of-scope file.

## Maintenance notes

- Event protocol versions are durable. Future fields must be additive or get a
  new version; old versions must never be reinterpreted with guessed lineage.
- If new orchestrator calls or pool retry mechanisms are added, they must set
  deterministic call identity and explicit shell relation before UI wording is
  extended.
- Keep recovery operator-triggered. Never move `/queries?json` discovery into a
  periodic refresh loop or relax unique matching to improve apparent success.
- If a new HTTP dependency is proposed later, re-evaluate timeout plumbing but
  retain verified TLS, host authorization, no redirects, body caps, and the
  read-only endpoint allowlist.
- Review sidecar tail logic whenever writer rotation/truncation behavior
  changes; current assumptions rely on append-only, newline-terminated events
  capped near 1 MiB.
- Review compact mode whenever Job Detail gains another panel. The 80x24 region
  assertions are a product contract, not snapshot decoration.
- Production canary/TLS/auth validation remains a Release Operator gate and is
  deliberately not authorized by this implementation plan.

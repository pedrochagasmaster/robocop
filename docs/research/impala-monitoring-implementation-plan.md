# Impala monitoring: implementation plan

Plan date: 2026-07-15
Basis: [impala-debug-web-monitoring-contract.md](impala-debug-web-monitoring-contract.md) (authoritative research record — endpoint semantics, state tables, probe evidence, and rationale live there and are not restated here), `AGENTS.md`, `CONTRIBUTING.md`, [ADR-0005](../adr/0005-scr-modification-policy.md), and the 2026-07-15 dispatch handoff.
Status baseline: branch `research/impala-monitoring-json` at `e85cf1f`; no product code changed; full suite `644 passed, 25 skipped`.

## Goal

Dispatch shows queued / running / failed / retried / completed status for the
Impala work behind each job, driven by exact `(coordinator URL, query ID)`
identities captured at the `impala-shell` seam and read-only `?json` polling —
never HTML scraping, never write endpoints, never a change to execution
behaviour.

## Non-goals

- Query cancellation through the debug web server (explicitly excluded; the
  existing job-cancel path via the runner is untouched).
- Changing manifest states, orchestrator CLI contracts, pool order, retry
  timing, email behaviour, or CSV handling.
- A cluster-wide query browser. Monitoring is scoped to Dispatch's own jobs.
- Storing monitoring identities/observations in `manifest.json` (its coarse
  states stay authoritative and unchanged).

## Fixed decisions (from the handoff — do not re-litigate)

Adapter-only `?json` consumption; identity captured from `impala-shell` output
with SQL/user/time matching as refusing-when-ambiguous recovery only; manifest
stays the final truth; explicit five-level hierarchy (job → orchestrator call →
shell execution → Impala query → transparent-retry query); `/backends?json`
cached coordinator discovery; `/query_stmt?json` first with capability
fallback; strictly read-only; all network work off Textual's event loop.

## Slice map

Six slices, one reviewable PR each, in dependency order. Slices 1 and 3 are
pure `dispatch/` additions. Slice 2 is the only `scr/` change and carries the
full ADR-0005 process. Environment gates (below) interleave; none block
starting slice 1 today.

```text
Slice 1  fixtures + pure parser/validator        dispatch/impala_monitor.py (pure part)
Slice 2  identity event protocol                 scr/_common.py + scr call sites + runner env  [scr/]
Slice 3  read-only HTTP adapter                  dispatch/impala_monitor.py (transport part)
Slice 4  background monitor service              dispatch/monitor_service.py
Slice 5  TUI presentation                        dispatch/screens/job_detail.py
Slice 6  production canary + hardening           no new code; evidence + fixes
```

Per-slice branches off refreshed GitHub `main` (`git switch main && git pull
--ff-only`, then e.g. `feat/impala-monitor-parser`). Every slice ends with
`.venv\Scripts\python.exe -m pytest -q -n 4 --dist loadfile`, `ruff check`,
`ruff format --check`, `mypy` clean, and a PR when authorized. The two known
`test_install_onboarding.py` failures on clean checkouts (AGENTS.md) are
pre-existing and out of scope.

---

## Slice 1 — Sanitized contract fixtures + pure parser and URL validator

**Branch:** `feat/impala-monitor-parser`. **No network, no UI, no scr/.**
Test-first per the `tdd` skill: write each fixture and its failing test before
the parser code that satisfies it.

### 1a. Fixture sanitization protocol

Derive minimal JSON fixtures from `docs/monitoring/` (raw, sensitive,
uncommitted — preserve as-is) and the live probe shapes recorded in the
research note. New directory `tests/fixtures/impala_monitor/`. Sanitization
rules, applied by hand and re-checked before commit:

- Hostnames → `coordinator-1.internal.example:25443` (and `-2`, `-3` for
  discovery fixtures).
- Query IDs → synthetic hex pairs, e.g. `1a2b3c4d5e6f7081:9192a3b4c5d6e7f8`.
- `stmt` → `SELECT 1 /* sanitized */` (≤ 40 chars); one discovery fixture keeps
  a 250-char synthetic statement plus ellipsis to encode the truncation fact.
- Users → `user_a`; databases → `db_a`; pools keep only the real pool *names*
  (`default`, `adhoc_small`, `adhoc` are not sensitive and matter for tests).
- Error text → one-line synthetic messages per category; no stack traces.
- Keep every structural key observed on the deployed build (`record_json`
  nesting, `inflight`/`not_inflight`, human-formatted duration/bytes strings,
  `queued_duration` present / `query_progress` absent) so the fixtures encode
  the *deployed* shape, not upstream master's.

Fixture inventory (file → contract point):

| Fixture | Encodes |
|---|---|
| `query_stmt_created.json` | `CREATED`, planning |
| `query_stmt_queued.json` | `COMPILED` + `last_event: "Queued"` + `queued_duration` |
| `query_stmt_compiled_not_queued.json` | `COMPILED` without `Queued` event → preparing |
| `query_stmt_running.json` | `RUNNING`, `progress` string, bytes fields |
| `query_stmt_finished.json` | `FINISHED`, `inflight` true (open for fetch) |
| `query_stmt_exception.json` | `EXCEPTION` + top-level `status` error text |
| `query_stmt_retrying.json`, `query_stmt_retried.json` | retry states (synthetic — flagged `"_synthetic": true` until gate 3 captures real ones) |
| `query_stmt_archived.json` | `not_inflight` archive lookup |
| `query_stmt_plan_metadata_unavailable.json` | early-planning transient, no `record_json` |
| `query_stmt_unknown_id_error.json` | top-level `error: "Unknown query id"` on HTTP 200 |
| `query_stmt_future_state.json` | unknown `state` value + unknown extra keys |
| `query_stmt_minimal.json` | only `query_id` + `state` — every optional field absent |
| `queries_list.json` | `in_flight_queries`/`completed_queries` arrays, truncated stmts, same-prefix ambiguous pair |
| `backends.json` | mixed coordinators/executors, `webserver_url`, `is_active` false entry |
| `malformed.txt`, `not_object.json` | non-JSON / JSON-but-not-object bodies |

Oversized-payload cases are generated in-test (repeat a filler key), not
checked in. A test asserts no fixture contains `mastercard`, a real hostname
pattern, or a statement longer than 260 chars — a cheap tripwire against
future fixture additions leaking captures.

### 1b. `dispatch/impala_monitor.py` — pure layer

Frozen dataclasses mirroring the research note's proposed boundary:

```python
Relation = Literal["initial", "transparent_retry"]
Phase = Literal["preparing", "queued", "running", "succeeded", "failed", "retrying", "unknown"]

@dataclass(frozen=True)
class QueryIdentity:
    coordinator_base_url: str   # scheme+host+port only, validated
    query_id: str               # validated hex:hex
    shell_execution_id: str
    relation: Relation
    discovered_at: str          # ISO-8601 UTC

@dataclass(frozen=True)
class ProgressCounter:
    completed: int | None
    total: int | None
    display: str                # original string, always retained

@dataclass(frozen=True)
class ImpalaObservation:
    raw_state: str | None
    phase: Phase
    pool: str | None
    scan_progress: ProgressCounter | None
    query_progress: ProgressCounter | None
    queued_duration: str | None
    bytes_read: str | None
    rows_fetched: int | None
    last_event: str | None
    status_summary: str | None  # one line, truncated, never full error body
    detail_url: str
    observed_at: str
    availability_error: str | None  # set for plan_metadata_unavailable / unknown-id / parse failure
```

Pure functions (no I/O, no logging of payload bodies):

- `parse_query_detail(payload: bytes, identity) -> ImpalaObservation` — size
  guard first (reject > `MAX_BODY_BYTES`, default 4 MiB; observed detail pages
  are ~80 KB), then `json.loads` with all failures mapped to an observation
  carrying `availability_error`, never an exception that escapes to callers.
  Handles: top-level `error` (evicted/unknown ID → `availability_error`,
  phase preserved from prior knowledge by the *service*, not invented here),
  `plan_metadata_unavailable` (→ phase `preparing`, `availability_error`
  `"plan metadata unavailable"`), missing `record_json`, `not_inflight`
  archive records, unknown extra keys ignored, all listed fields optional.
- `map_state(raw_state, last_event) -> Phase` — exactly the research note's
  table: `CREATED`→preparing; `COMPILED`→queued iff `last_event == "Queued"`
  else preparing; `RUNNING`→running; `FINISHED`→succeeded; `EXCEPTION`→failed;
  `RETRYING`/`RETRIED`→retrying; anything else →unknown with `raw_state`
  preserved. Phase names an *attempt* outcome; job truth stays with the
  manifest (enforced in slices 4–5, documented here).
- `parse_progress(display) -> ProgressCounter` — defensive `n / total` parse;
  `N/A`, empty, or unparseable keeps `display` with `None` counts.
- `validate_coordinator_url(url) -> str` — normalizes and returns base URL or
  raises `IdentityValidationError`: scheme must be `https` (plus `http` only
  when an explicit dev/mock flag is set by config), no userinfo, no path/query
  /fragment beyond `/`, hostname syntactically valid, port present or default.
- `validate_query_id(qid)` — anchored `^[0-9a-f]{16}:[0-9a-f]{16}$`.
- `build_detail_url(base, endpoint, query_id)` — endpoint restricted to the
  enum {`query_stmt`, `query_summary`, `query_plan_text`, `query_plan`}; there
  is deliberately no function that fetches or builds an arbitrary path, and a
  test asserts `cancel_query` cannot be produced.

### 1c. Tests

`tests/test_impala_monitor.py`, table-driven over the fixture inventory, plus
property-style edge tests (every field deleted one at a time from the running
fixture must still parse). Target: each row of the state table, each
availability path, each validator rejection (including `/cancel_query`,
`file://`, credentials-in-URL, redirect-bait hosts embedded in the URL string,
uppercase/short/long query IDs).

**Done when:** suite green; module imports no HTTP/Textual symbols (asserted
by a test inspecting `sys.modules` after import, mirroring the codebase-design
"deep module" intent); PR contains only `dispatch/impala_monitor.py`, fixtures,
and tests.

---

## Slice 2 — Execution identity event protocol (`[scr/]` change)

**Branch:** `feat/impala-identity-events`. The only slice touching `scr/`;
scope is exactly the research note's "Dispatch-specific execution seam"
design. ADR-0005 process applies in full.

### 2a. Changes

1. **`dispatch/runner.py`** — extend `_orchestrator_env` (or a sibling) so
   every orchestrator subprocess gets `DISPATCH_MONITOR_EVENTS_PATH`
   (job-scoped file, `<job_dir>/monitor.events.jsonl`) and
   `DISPATCH_JOB_ID`. This mirrors the existing `DISPATCH_REQUEST_POOL`
   precedent and does not change argv.
2. **`scr/_common.py`** — new stdlib-only helper, e.g.
   `run_impala_shell(argv, *, pool) -> tuple[int, bytes, bytes]`:
   - Spawns `impala-shell` and drains stdout and stderr **concurrently**
     (two reader threads) — required to avoid the deadlock the research note
     flags; returns the exact bytes `communicate()` returns today so caller
     classification (`classificar_erro_impala`) sees identical input.
   - While draining stderr, recognizes only two anchored line shapes from the
     shell source: the `Query state can be monitored at: <url>` monitor line
     and the `Retried query link: <url>` line. Everything else passes through
     uninterpreted. Patterns are version-stamped constants so gate-2 captures
     can adjust them in one place.
   - Emits bounded JSON Lines events to `DISPATCH_MONITOR_EVENTS_PATH` when
     set: `shell_started`, `query_discovered`, `query_retried`,
     `shell_finished` — each with `v: 1`, `job_id`, generated
     `shell_execution_id`, `seq`, `pool`, UTC timestamp, and for discovery the
     validated coordinator base URL + query ID. Never SQL, never error bodies.
   - Every monitoring action is wrapped so no exception, full disk, or bad
     path can alter the child's exit code, output bytes, or timing beyond the
     drain itself. Missing env var → helper degrades to plain drain.
3. **Call sites** — replace `Popen(...).communicate()` at
   `scr/Query_Impala_Parametrized.py:109` and `scr/download_to_csv.py:61` with
   the helper (both Impala launch functions; `monthly_query_processor` flows
   through the pinned single-shell path that reuses them). Argv, env
   inheritance, and return handling unchanged.
4. **Mock layer** — extend `mocks/bin/impala-shell` to print a realistic
   monitor line (and a retried-link line under a new scenario flag) so the
   protocol is exercised end-to-end by the existing scenario machinery. Add
   scenarios `monitor_line.json` and `transparent_retry.json`; existing 16
   scenarios stay byte-compatible.

### 2b. ADR-0005 compliance sequence

1. Capture pre-change stdout/stderr/exit/email/CSV artifacts for **all**
   `mocks/scenarios/*.json` with the *current* mock.
2. Apply the `scr/` + runner change; re-capture with the same unmodified mock;
   attach side-by-side equivalence in the PR.
3. Only then land the mock extension + new scenarios in the same PR, with new
   assertions on the sidecar file.
4. PR carries the `[scr/]` tag and the what/why-safe/regression-risk
   paragraph; two reviewers, one with production experience of the scripts.

Preserved invariants (asserted by tests where possible): stdout/stderr bytes,
exit codes, error classification inputs, email behaviour, frozen pool list and
retry timing, CSV temp-file handling, no new imports beyond the standard
library, no change to the public argv surface.

**Done when:** all scenarios green pre/post, equivalence attached, new
`tests/test_scr_common.py` cases cover deadlock (multi-MB stdout), monitor-line
extraction, malformed URL rejection (validation logic duplicated minimally in
`scr/_common.py` since it cannot import `dispatch`), events-path-unset
degradation, and unwritable events path harmlessness.

---

## Slice 3 — Read-only HTTP adapter

**Branch:** `feat/impala-monitor-http`. Extends `dispatch/impala_monitor.py`
with the transport half; the pure layer from slice 1 is unchanged.

- **No new dependency.** Use `urllib.request` over an injected
  `Transport` protocol (`fetch(url, timeout) -> (status, content_type,
  body_bytes)`), so all tests run against fakes and the stdlib path gets one
  thin integration test. Blocking calls run in threads
  (`asyncio.to_thread`) from callers — nothing here touches Textual.
- **TLS:** `ssl.create_default_context()` honoring a configured CA bundle path
  from `dispatch/config.py`; verification is never disabled in product code.
  Auth posture is pluggable-but-empty for now (deployed coordinator showed no
  Web-UI auth); an auth failure degrades to `availability_error =
  "monitoring unavailable"`, per the research note's security posture.
- **`ImpalaMonitorClient`** public surface (and nothing else):
  - `observe(identity) -> ImpalaObservation` — targeted poll. Capability
    detection per coordinator: try `/query_stmt?json` once, fall back to
    `/query_plan?json` on 404/unknown-shape, cache the choice.
  - `discover_coordinators(seed_base_url) -> list[str]` — `/backends?json`,
    filter `is_coordinator and is_active`, cache with TTL (default 10 min);
    never refetched per observation tick.
  - `discover(criteria) -> QueryIdentity` — bounded recovery sweep of cached
    coordinators' `/queries?json` matching user + start window + statement
    prefix + type + db; raises `AmbiguousIdentityError` on zero or >1 match.
    Operator-triggered only (wired in slice 4/5), never on a refresh loop.
- **Hard limits enforced before parsing:** host must be in the approved set
  (seed + discovered coordinators), redirects not followed cross-host,
  `Content-Type` must be JSON, body capped at `MAX_BODY_BYTES`, connect/read
  timeouts (defaults 3 s / 10 s), bounded retries with jitter, per-coordinator
  circuit breaker (open after N consecutive failures, half-open probe).
- Tests: fake transport replaying slice-1 fixtures plus adversarial cases —
  redirect to off-list host, oversized body, wrong content type, HTML body,
  TLS failure surfaced as availability error, capability fallback, breaker
  behaviour, and an explicit test that no code path can issue a request whose
  path contains `cancel_query`.

**Done when:** adapter is the only module that knows wire shapes; UI/service
layers (next slices) consume only `QueryIdentity`/`ImpalaObservation`.

---

## Slice 4 — Background monitor service

**Branch:** `feat/impala-monitor-service`. New `dispatch/monitor_service.py`.

- **Input:** tails `<job_dir>/monitor.events.jsonl` (tolerant of partial last
  line, unknown event versions, and absent file = monitoring unavailable) and
  builds the explicit hierarchy: shell executions (with pool + relation) →
  queries → transparent-retry queries. A second monitor URL inside one shell
  is the next statement (sibling query); a new shell on another pool is an
  orchestrator fallback (sibling shell). Never conflate with Impala retry.
- **Polling policy:** one poller per live query shared across all consumers;
  foreground cadence 2 s only while a Job Detail screen subscribes, background
  30 s otherwise, stop after one confirmed-terminal observation (poll once
  more, persist, stop). Disappearance (unknown-id/evicted) retains the last
  good observation with `availability_error` — never synthesized into
  success/failure. Job-level truth always re-read from the manifest.
- **Threading model:** service owns a small worker (thread or asyncio task
  started from the app), publishes immutable snapshots the UI reads; no
  Textual imports in the module.
- Tests: event-file replay (including interleaved multi-statement monthly
  shape and pool-hop chain from the research note's regression list), poller
  sharing, cadence switching, terminal-stop, eviction handling, TUI-restart
  recovery (service rebuilt purely from the sidecar + one poll), and the
  invariant that no code path writes anywhere except its own state.

---

## Slice 5 — TUI presentation

**Branch:** `feat/impala-monitor-ui`. **Mandatory:** follow
`.agents/skills/dispatch-textual-tui/SKILL.md` before touching
`dispatch/screens/`.

- `job_detail.py` gains a monitoring panel fed by the service snapshots via
  the screen's existing refresh pattern: logical job state (manifest) shown
  *separately* from current Impala attempt phase, pool, reported progress
  (worded as "reported work completed", never ETA; original display string
  shown), queued duration, and a compact shell/query attempt history
  (`attempt failed; job retrying` phrasing for mid-chain exceptions).
- Subscription drives the service's foreground cadence; hiding the screen
  drops it back. All I/O stays in workers; the screen only paints snapshots
  (respect the existing `_static_cache` no-op repaint pattern).
- Tests: snapshot tests in `tests/test_ui_snapshots.py` for the panel states
  (monitoring unavailable, queued, running with progress, retry chain,
  attempt-failed-job-retrying), plus a test that monitoring failure leaves
  cancel/log-tail behaviour untouched.

---

## Slice 6 — Production canary and hardening

No new feature code. From the Edge Node (use `impala-via-tmux`), run the four
research-note canaries — a queued query, a long runner with advancing
progress, a controlled pool-fallback failure, and two same-prefix ambiguous
queries — and verify all execution outcomes are unchanged with monitoring on.
Fold discrepancies back as fixture updates (slice 1) or adapter fixes
(slice 3). Update the research note only if evidence changes a decision.

---

## Environment gates → slice dependencies

From the research note's gate list (its wording is authoritative):

| Gate | Blocks | Until then |
|---|---|---|
| 1. TLS/auth from real Edge Node process, verification **on** | slice 3 defaults being trusted; slice 6 | develop slice 3 fully against injected transport |
| 2. Production shell monitor/retry line variants, byte-preserved | finalizing slice 2 regex constants | implement from upstream shell source; constants isolated for a one-line fix |
| 3. Sanitized records for every deployed transition (esp. RETRYING/RETRIED) | de-flagging synthetic fixtures | synthetic fixtures marked `_synthetic` |
| 4. Coordinator restart/eviction behaviour; realistic poll cadence | slice 4 tuning | defaults 2 s / 30 s from the note |
| 5. Same-prefix ambiguity proof | slice 6 sign-off | discovery already refuses ambiguity by construction |

Gate work is read-only probing and can proceed in parallel with slices 1–3.

## Cross-cutting guardrails (checked at every PR)

- `docs/monitoring/` and `docs/research/` stay uncommitted until explicitly
  authorized; nothing sensitive (hosts, query IDs, SQL, users, captures,
  credentials) enters git — the slice-1 tripwire test plus manual review.
- `scr/` remains standard-library-only; slice 2 is its only change.
- Monitoring can never alter execution: no writes outside its own sidecar/
  state, no `/cancel_query`, failures degrade to "monitoring unavailable".
- No `verify=False` anywhere in product code.
- Full test command before every PR; CI must pass on Python 3.10 and 3.12.

## Risks and mitigations

- **Deployed JSON drifts from fixtures** — capability detection + optional
  fields everywhere + fixtures encoding the deployed (not upstream) shape;
  canary catches residue.
- **Pipe-drain regression in `scr/`** (the highest-blast-radius change) —
  smallest possible diff, byte-equivalence proof, deadlock test with multi-MB
  output, monitoring wrapped so it cannot raise into the caller.
- **Polling load on coordinators** — per-query shared poller, background
  backoff, terminal-stop, cached discovery, circuit breaker.
- **Ambiguous identity after TUI restart** — sidecar JSONL is the durable
  identity record; recovery discovery refuses non-unique matches by design.

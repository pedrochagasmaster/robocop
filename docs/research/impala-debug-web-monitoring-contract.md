# Impala debug-web monitoring: contract and solution shape

Research date: 2026-07-15
Primary sources: Apache Impala documentation, source code, and ASF JIRA only. Source-code links are pinned to Apache Impala commit [`371cad3e`](https://github.com/apache/impala/commit/371cad3e015e48a80b32de0104491b2fd4696e29) unless a released version is named.

## Conclusion

Dispatch can provide useful queued/running/failed/completed monitoring from Impala's debug web server without scraping HTML. The JSON is the same server-produced document that the Mustache pages consume: the generic webserver renders any templated URL as JSON whenever the `json` query argument is present ([implementation](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/util/webserver.cc#L1263-L1285)).

The safest design is **attempt identity first, polling second**:

1. Capture the exact coordinator web address and query ID from the `impala-shell` execution seam.
2. Record every physical attempt under one Dispatch job.
3. Poll only that coordinator and query ID.
4. Treat `/queries?json` SQL/time matching as bounded fallback discovery, never the primary identity mechanism.
5. Preserve the raw Impala state and derive a small Dispatch-facing phase from it.
6. Keep the Dispatch manifest/process authoritative for the job's final outcome.

The key reason for step 1 is not just efficiency. `/queries?json` applies Impala redaction and, by default in current source, truncates statements to 250 characters ([handler](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-http-handler.cc#L586-L600), [flag](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-server.cc#L173-L183)). Concurrent queries with the same prefix can therefore be indistinguishable by statement, user, and a coarse formatted start time.

### Decision in one sentence

Build a **read-only, version-tolerant JSON adapter**, feed it exact
`(coordinator URL, query ID)` identities captured from `impala-shell`, and
model pool fallback as a chain of shell executions beneath the existing
Dispatch job. Do not scrape DOM elements, scan every coordinator on each UI
tick, or let an Impala attempt overwrite the manifest's logical job state.

## What was proven against the deployed cluster

The following read-only probes were run on 2026-07-15 against one production
coordinator and, where stated, the coordinator set it advertised. Hostnames,
query IDs, users, SQL, and full error text were deliberately not retained in
this note.

- The deployed build reports `Impala 4.0.0.7.1.9.1081-1`, a downstream build
  whose JSON shape is newer than the upstream 4.0.0 tag in some respects and
  older than current `master` in others. Capability detection is therefore a
  requirement, not defensive polish.
- Adding `?json` returned `application/json` for `/`, `/varz`, `/backends`,
  `/queries`, `/query_stmt`, `/query_plan_text`, `/query_summary`, and
  `/query_plan`. This confirms the useful data is generated server-side; it is
  not necessary to fetch HTML and locate cells.
- `/backends?json` listed 688 active backends, including 16 coordinators, with
  an HTTPS webserver URL and coordinator/executor flags for each. One known
  coordinator can therefore bootstrap the current coordinator set; the
  checked-in product does not need a manually maintained 16-host file.
- The coordinator flags observed were `query_log_size=100` and
  `query_stmt_size=250`. In one `/queries?json` sample, the longest statement
  was 253 characters (250 plus ellipsis), confirming that statement matching
  cannot be the primary identity contract.
- A bounded four-worker sweep reached all 16 coordinators, transferred about
  2.5 MB, and individual responses took roughly 1.3-4.6 seconds. That is
  acceptable as operator-triggered recovery discovery and inappropriate for a
  one- or two-second refresh loop.
- For one retained query, the deployed payloads were approximately 68.7 KB for
  `/query_stmt?json`, 68.7 KB for `/query_plan_text?json`, 74.7 KB for
  `/query_summary?json`, and 79.6 KB for `/query_plan?json`. All included
  `record_json`; only the last included `plan_json`. `/query_stmt` is the best
  currently-proven targeted polling candidate, but it is still a debug-page
  document rather than a small status resource.
- The deployed `record_json` contained `queued_duration`, `bytes_read`, and
  `bytes_sent`, but not current upstream's separate `query_progress`. Missing
  fields must remain normal.
- A same-user sweep observed retained `EXCEPTION` records across `default`,
  `adhoc_small`, and `adhoc`, plus a short-lived in-flight record that vanished
  before a follow-up detail poll. This directly demonstrates both the pool-hop
  attempt chain and why disappearance cannot be interpreted as a result.
- The sampled coordinator used HTTPS but had no Web-UI LDAP, SPNEGO, or
  password-file authentication enabled. Product code must still validate TLS
  and tolerate a different authentication posture on another environment; the
  exploratory client used disabled verification only to inspect the internal
  endpoint and is not an implementation precedent.

The supplied saved pages reinforce the same conclusion. They are rendered with
the status record already present and include JavaScript that mutates cells from
a supplied `record_json`; they do not define a separate stable browser REST
client to reuse. The generic server-side `?json` mode is the seam to isolate.
The raw captures also contain full SQL, internal hostnames, and query IDs, so
`docs/monitoring/` must remain uncommitted and must be replaced by minimal,
sanitized JSON fixtures before a pull request.

## What the endpoints actually are

The Impala docs call port 25000 an administrator **debug Web UI**, and explicitly warn that its items and formats are subject to change ([Web UI docs](https://impala.apache.org/docs/build/html/topics/impala_webui.html)). This is an implementation contract, not a versioned public REST API. It should sit behind one compatibility adapter with fixture tests.

Impala registers the relevant callbacks as follows ([source](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-http-handler.cc#L132-L203)):

- `GET /queries?json` invokes `QueryStateHandler` and returns arrays named `in_flight_queries` and `completed_queries` plus counts, configured archive limits, query-location data, common page metadata, and tooltip strings.
- `GET /query_plan?query_id=<id>&json` invokes `QuerySummaryHandler(include_json_plan=true, include_summary=true)`.
- `GET /query_summary?query_id=<id>&json` invokes the same handler without the JSON plan.
- `GET /query_stmt?query_id=<id>&json` and `/query_plan_text?...&json` invoke the same handler without plan JSON or execution summary.

For a current Impala query record, `QueryStateToJson()` emits these useful fields ([source and definitions](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-http-handler.cc#L586-L716)):

| Field | Meaning / caveat |
|---|---|
| `query_id` | Exact physical-attempt ID. |
| `effective_user`, `default_db`, `stmt`, `stmt_type` | Discovery metadata. `stmt` is redacted and is truncated on `/queries`. |
| `state` | Web-facing state; current releases can also expose retry states. Preserve unknown values. |
| `last_event` | Last label in the query event timeline; optional when there are no events. |
| `resource_pool` | Resolved pool for queries subject to admission control. |
| `queued_duration`, `duration`, `waiting_time` | Human-formatted strings, not numeric durations. |
| `progress` | Current source: completed scan ranges / total scan ranges, formatted as text. |
| `query_progress` | Current source: completed fragment instances / total fragment instances, formatted as text. |
| `bytes_read`, `bytes_sent`, `mem_usage`, `mem_est` | Human-formatted strings. |
| `rows_fetched` | Rows already fetched by the client, not rows produced internally. |
| `inflight` / `not_inflight` | Top-level fields on query-detail JSON, indicating active registration versus archive lookup. |
| `status` | Top-level `OK` or the redacted query error text. |
| `record_json` | The query record nested in query-detail JSON. |

There are two non-HTTP-error responses the client must handle explicitly:

- During early planning, query-detail JSON can contain `plan_metadata_unavailable` and omit `record_json` ([source](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-http-handler.cc#L1380-L1395)). This is a transient state, not a failed query.
- An evicted or unknown ID produces a JSON `error` field (`Unknown query id`) from the handler ([source](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-http-handler.cc#L1416-L1421)). The browser code checks the JSON field rather than relying on a non-200 status ([template](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/www/query_plan.tmpl#L181-L195)).

Do not assume JSON array order. The handler builds the active and archived collections differently; select by query ID.

## State semantics

Impala's internal execution state machine is:

`INITIALIZED -> PENDING -> RUNNING -> FINISHED`, with any state able to transition to `ERROR`. DDL can skip `PENDING` ([source](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/client-request-state.h#L62-L69)). The Web UI maps those states to Beeswax names ([mapping](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/client-request-state.cc#L2129-L2138)):

| Web `state` | Safe interpretation |
|---|---|
| `CREATED` | Planning/initialization. |
| `COMPILED` | Internal `PENDING`; not yet executing. Calling it *queued* is strongest when `last_event == "Queued"`. |
| `RUNNING` | Executing. |
| `FINISHED` | Impala execution finished successfully. Client fetching/close and later Dispatch phases may remain. |
| `EXCEPTION` | This Impala attempt errored or was cancelled. It does not prove the Dispatch job is terminal. |
| `RETRYING`, `RETRIED` | Current Web UI can substitute retry state for the original attempt's execution state ([record construction](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/query-state-record.cc#L105-L111)). |
| anything else | Preserve raw value and show `unknown`, rather than treating it as failure. |

`waiting` and `executing` are **UI table categories, not an execution-state API**. The handler sets `waiting` when an active query is in `EXCEPTION` or has returned all rows, and `executing` to the inverse ([source](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-http-handler.cc#L697-L709)). A monitor should derive its phase from `state`, not from these booleans.

The `Queued` event is a real admission-control timeline event ([source](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/scheduling/admission-control-client.cc#L28-L33)). For richer queue diagnosis, `/admission?json` can expose queued-query and reason information where that endpoint is enabled, but it is optional enrichment rather than the core job monitor ([Web UI docs](https://impala.apache.org/docs/build/html/topics/impala_webui.html)).

## Progress is a counter, not an ETA

Current `progress` is scan-range completion and `query_progress` is fragment-instance completion. They answer “how much scheduled scan/fragment work has completed,” not “how much wall-clock time remains.” Apache's `LIVE_PROGRESS` documentation explicitly says scan progress can reach 100% while aggregation or other work remains ([docs](https://impala.apache.org/docs/build/html/topics/impala_live_progress.html)). Some statements have no coordinator progress and report `N/A`; `COMPUTE STATS` has documented live-progress limitations.

The field shape has changed across releases. In Apache Impala 3.4.2, `progress` counted completed plan fragments and fields such as `query_progress`, `bytes_read`, and `queued_duration` were absent ([3.4.2 source](https://github.com/apache/impala/blob/cbb6fa1cf2de5007751628677380a51f70052b53/be/src/service/impala-http-handler.cc#L362-L443)); in 4.3, `progress` is scan ranges and `query_progress` is fragment instances ([4.3 source](https://github.com/apache/impala/blob/14bb13e67e48742df72f9e1dd73be15ec7ba31bd/be/src/service/impala-http-handler.cc#L507-L617)). Therefore:

- detect fields by presence;
- parse `n / total` defensively while retaining the original display string;
- label the metric (`scan` versus `query`) based on a version-tested adapter;
- never use 100% as the terminal signal; use `state` and then the Dispatch manifest.

## Query identity and coordinator discovery

There is no cluster-wide query registry in the per-impalad debug UI. Official docs state that each host's UI contains details for queries for which that host served as coordinator ([docs](https://impala.apache.org/docs/build/html/topics/impala_webui.html)). Scanning every coordinator is therefore only a fallback.

Do not mistake `/queries?json`'s `query_locations` array for coordinator
discovery. Source and live shape show backend fragment-location/count records
for queries known to the current coordinator. `/backends?json` is the useful
bootstrap surface because each entry carries `webserver_url`, `is_coordinator`,
`is_executor`, and `is_active`
([handler](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-http-handler.cc));
cache its filtered active-coordinator set rather than refetching the roughly
489 KB production document on every observation.

`impala-shell` already gets the exact webserver address from the connected impalad's Ping RPC ([client](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/shell/impala_shell/impala_client.py#L734-L742), [server](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-hs2-server.cc#L1458-L1484)). Once execution returns a handle, the shell formats the exact URL as:

`<webserver_address>/query_plan?query_id=<query-id>`

([client](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/shell/impala_shell/impala_client.py#L302-L310)). In verbose mode it prints both the coordinator and `Query state can be monitored at: ...` immediately after submission ([shell](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/shell/impala_shell/impala_shell.py#L1485-L1516)). `--quiet` disables verbose output ([shell options](https://impala.apache.org/docs/build/html/topics/impala_shell_options.html)).

That monitor line should be turned into a structured Dispatch attempt event. If the current orchestration must remain text-only, parse the complete URL from stderr with a strict anchored pattern and retain the original log line. Avoid reconstructing the host or matching SQL.

Fallback discovery should be time-bounded and require a unique match over configured coordinator URLs using user, start window, statement prefix, statement type, and expected database. If zero or multiple candidates match, report “identity unavailable/ambiguous”; never guess.

## Retries and pool hopping

Impala 4.0 introduced transparent retries for eligible `SELECT` queries after cluster-membership changes. They are off by default, limited to one retry, and skipped once rows have been returned ([query option docs](https://impala.apache.org/docs/build/html/topics/impala_retry_failed_queries.html), [source](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/runtime/query-driver.h#L156-L174)). Each retry is a new query attempt with a new ID and its own profile, while the original becomes `RETRYING`/`RETRIED` ([design in source](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/runtime/query-driver.h#L91-L133)). The shell can append a `Retried query link` by extracting the new ID from the server log message ([client](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/shell/impala_shell/impala_client.py#L418-L427)).

**Inference:** built-in transparent retry remains owned by the same `ImpalaServer`/`QueryDriver`, so its coordinator web address remains the same even though the query ID changes. A sequence that launches a fresh shell query in another requested pool (and possibly through a load balancer to another coordinator) is a separate orchestration retry, not an Impala-linked retry. Impala cannot supply a logical cross-submission job ID for that sequence. Dispatch's runner must record the attempt chain explicitly.

The model should consequently distinguish:

- Dispatch job state;
- physical Impala attempt state;
- attempt relation (`initial`, `impala-transparent-retry`, `orchestrator-pool-fallback`);
- coordinator URL, query ID, pool, and timestamps per attempt.

An `EXCEPTION` on attempt 1 followed by a new attempt must display as “attempt failed; job retrying,” not “job failed.”

## Retention, disappearance, and completion

Active queries remain in `in_flight_queries` until they are closed/unregistered, including queries that are no longer executing but remain available for result inspection. Archived records are a bounded in-memory FIFO. Current defaults are 200 records and 2 GiB, with the smaller limit winning; either `0` disables archiving and `-1` makes that dimension unbounded ([flags](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-server.cc#L173-L180), [eviction](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-server.cc#L1237-L1263)). `/queries?json` reports the configured limits, but not a time-to-live.

Consequences:

- a query can move from active to archived without disappearing;
- it can later disappear because of count/byte eviction or coordinator restart;
- disappearance is **not** evidence of success or failure;
- persist the last valid observation and use the runner/manifest for terminal truth;
- poll a terminal attempt once more, store the final observation, then stop.

## Security and transport

The debug UI is an administrative surface containing query text, users, plans, errors, and resource usage. It can be disabled with `--enable_webserver=false`, listens on port 25000 by default, and can be protected by SPNEGO ([Web UI docs](https://impala.apache.org/docs/build/html/topics/impala_webui.html)), htpasswd, LDAP, and TLS ([security docs](https://impala.apache.org/docs/build/html/topics/impala_security_webui.html), [current flags](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/util/webserver.cc#L90-L149)). Web-UI authentication is separate from the HS2/Beeswax client session: a browser working through SSO does not prove a background Python HTTP client can authenticate.

Required posture:

- use HTTPS when configured and validate against a configured CA/system trust; do not ship `verify=False`;
- support the actual deployed mode (likely SPNEGO on a Kerberized cluster) or degrade to “monitoring unavailable”;
- use short connect/read timeouts, bounded retries with jitter, and a per-coordinator circuit breaker;
- never log credentials, cookies, full response bodies, or unredacted error text;
- treat coordinator hostnames and query IDs as internal data.

The cancellation endpoint deserves special caution. `GET /cancel_query?query_id=<id>` unregisters/cancels the query. The source explicitly says the Web UI has no query secret and assumes the Web UI is allowed to close queries ([handler](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/be/src/service/impala-http-handler.cc#L264-L284)). Monitoring should be read-only and must not expose a generic URL fetcher. Cancellation, if ever added, should be a separate authorized feature using the client protocol and explicit confirmation, not this debug GET.

## Polling cost and endpoint choice

The official Plan page polls `/query_plan?...&json` every two seconds ([template](https://github.com/apache/impala/blob/371cad3e015e48a80b32de0104491b2fd4696e29/www/query_plan.tmpl#L269-L296)); the Summary page polls `/query_summary?...&json` every second. That establishes that targeted polling is intended, but the payloads are not cheap: query-plan polling rebuilds plan JSON and summary text, while `/queries?json` serializes all active and archived records.

Recommended production behavior:

- discovery: at most one bounded `/queries?json` pass over configured coordinators when no captured identity exists;
- targeted polling: start at 2 seconds for a visible Job Detail screen, back off when hidden/backgrounded, and share a single poll per attempt among UI consumers;
- prefer the lightest query-detail endpoint proven compatible with the deployed Impala version. `/query_summary?...&json` avoids `plan_json`; `/query_stmt?...&json` is lighter still but must be verified against the deployed build. The already-observed `/query_plan?...&json` is the compatibility fallback;
- cap response size and validate `Content-Type` plus a minimal schema before parsing.

## Dispatch-specific execution seam

Today, `dispatch.runner` starts each manifest orchestrator with its output wired
to `run.log`, but both Impala launch functions inside `scr/` start
`impala-shell` with `stdout=PIPE`, `stderr=PIPE`, and wait in `communicate()`.
The shell's monitor URL is therefore captured inside the orchestrator and is
not available to Dispatch while the query runs. On a successful call it may
never reach `run.log` at all.

The existing invocations do not use `--quiet`, so upstream shell behavior says
the exact monitor URL is available. The narrowest reliable identity change is:

1. `dispatch.runner` always supplies a private, job-scoped monitoring-event
   path and job ID through environment variables. This does not change the
   orchestrators' public argv contract.
2. A standard-library helper in `scr/_common.py` drains both child pipes while
   preserving the bytes currently returned for success/error classification.
   It recognizes only the shell's fixed monitor/retried-link prefixes and a
   strict Impala query URL; it does not interpret general log text.
3. Each `impala-shell` process gets a generated shell-execution ID. The helper
   appends bounded JSON Lines events such as `shell_started`,
   `query_discovered`, `query_retried`, and `shell_finished`, including the
   known pool and a sequence number but no SQL or full error body.
4. `dispatch` consumes that sidecar as durable identity/lifecycle evidence and
   polls query status independently. A monitoring failure never changes the
   orchestrator return code or manifest.

This belongs under the existing [`scr/` modification
policy](../adr/0005-scr-modification-policy.md): it is a narrow observability
improvement, keeps the CLI/queues/retry timing/email behavior unchanged, and
requires every mock scenario plus side-by-side output equivalence. Concurrent
pipe draining is essential; reading only stderr line-by-line can deadlock when
stdout fills.

### Use three levels, not one overloaded status

A multi-statement monthly script can produce multiple query IDs in one shell,
and a `Table+Csv` job has two orchestrator calls. The durable model should be:

```text
Dispatch Job                       final truth: manifest/runner
  Orchestrator Call                existing manifest call
    Shell Execution                one impala-shell process, one requested pool
      Impala Query                 exact coordinator URL + query ID
        Transparent Retry Query    new query ID, same shell/coordinator lineage
```

A fresh shell on the next pool is a sibling **Shell Execution**, not an Impala
transparent retry. A second monitor URL within a monthly shell can be the next
statement, not a retry. This distinction prevents three otherwise likely UI
bugs: declaring the whole job failed on the first `EXCEPTION`, declaring it
succeeded when an early DDL reaches `FINISHED`, or drawing an unrelated next
statement as a retry.

### Module ownership

- `scr/_common.py`: capture shell execution/query identity and append the
  versioned event protocol. It knows no Textual or HTTP.
- `dispatch/impala_monitor.py` (new): own URL validation, JSON fetching,
  capability detection, state/progress parsing, retry/backoff, coordinator
  discovery, and sanitized observations. This is the deep compatibility
  module and primary fixture-test surface.
- `dispatch/screens/job_detail.py`: observe the module from a worker, display
  the current query plus compact pool/shell history, and stop foreground-rate
  polling when hidden. It remains an observer, not the durable lifecycle
  owner.
- `manifest.json`: retain its current coarse job states. Monitoring identities
  and observations do not belong in the manifest's state field.

The HTTP adapter must expose operations such as `observe(identity)` and a
bounded `discover(criteria)`, not an arbitrary URL fetcher. It must reject
`/cancel_query`, redirects to unapproved hosts, oversized bodies, wrong content
types, and malformed query IDs.

## Proposed module boundary

One deep monitoring module should hide the unstable wire shape. The UI-facing
observation can stay small even though the durable event model distinguishes
shell executions and queries:

```text
observe(attempt_identity) -> ImpalaObservation

QueryIdentity
  coordinator_base_url
  query_id
  shell_execution_id
  relation = initial | transparent_retry
  discovered_at

ImpalaObservation
  raw_state
  phase = preparing | queued | running | succeeded | failed | retrying | unknown
  pool
  scan_progress?        # completed, total, display
  query_progress?       # completed, total, display
  queued_duration?
  bytes_read?
  rows_fetched?
  last_event?
  status_summary?
  detail_url
  observed_at
  availability_error?
```

The adapter should accept missing/additional keys, retain raw strings, and never let malformed monitoring data affect query execution. Network work belongs off Textual's event loop. The UI can show the latest attempt prominently and retain a compact attempt history.

## Recommended implementation sequence

Each slice is independently reviewable and keeps the risky `scr/` change small:

1. **Contract fixtures and pure parser.** Check in sanitized queued, preparing,
   running, successful, failed, unknown-ID, and missing-field JSON fixtures.
   Implement state/progress normalization and URL validation with no network or
   UI.
2. **Identity event protocol.** Add the job-scoped JSONL protocol and strict
   monitor-line parser. Prove pipe output, exit codes, error classification,
   emails, CSV temp-file behavior, pool order, and retry timing are unchanged
   across every mock scenario required by ADR-0005.
3. **Read-only HTTP adapter.** Add injected transport, response limits,
   content/schema checks, TLS configuration, targeted `/query_stmt?json`
   polling with `/query_plan?json` compatibility fallback, and bounded
   `/backends?json` plus `/queries?json` recovery discovery that refuses zero or
   multiple matches.
4. **Background monitor integration.** Read identity events, share one poll per
   query among consumers, retain the last good observation through transient
   failures, and back off when no Job Detail screen is visible.
5. **TUI presentation.** Show logical job state separately from current Impala
   phase, pool, reported progress, queued duration, and a compact shell/query
   history. Phrase progress as reported work completed, never an ETA.
6. **Production canary.** From the Edge Node, exercise one query that queues,
   one that runs long enough to advance progress, one controlled failure that
   falls through pools, and two intentionally ambiguous same-prefix queries.
   Monitoring failure must leave all four execution outcomes unchanged.

The high-value regression cases are multi-statement monthly jobs, `Table+Csv`
jobs, transparent Impala retry, pool fallback, TUI restart mid-query,
coordinator restart/eviction, malformed or oversized JSON, TLS/auth failure,
and a captured URL that attempts to leave the approved coordinator set.

## Remaining environment gates before implementation

Most original questions were answered by source and live probes. These remaining
deployment-specific gates do not change the architecture:

1. From the actual Edge Node/Dispatch process identity, verify DNS, TCP 25000,
   the TLS chain **with verification enabled**, redirects, and authentication
   for a coordinator URL returned by `impala-shell`.
2. Capture the production shell's exact monitor and transparent-retry line
   variants while preserving stdout/stderr byte-for-byte, including a
   multi-statement monthly invocation.
3. Capture minimal sanitized records for `COMPILED + Queued`, `RUNNING`,
   `FINISHED`, `EXCEPTION`, and—if enabled—`RETRYING`/`RETRIED`. The current
   samples cover queued/failed HTML and live created/failed JSON but not every
   deployed transition.
4. Verify record behavior after an actual coordinator restart/eviction and
   establish the background/foreground polling intervals under realistic
   concurrent Dispatch usage.
5. Run two same-user queries with the same first 250 SQL characters to prove
   exact identity capture and confirm fallback discovery refuses ambiguity.

After those probes, implementation risk is localized to the HTTP/auth adapter and the execution-to-attempt identity seam; the TUI state model is straightforward.

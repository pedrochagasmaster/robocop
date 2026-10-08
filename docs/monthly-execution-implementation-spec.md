# Monthly execution: implementation specification

Status: implementation-ready proposal; no runtime changes implemented by this document.
Baseline: `3d45128` on `origin/main`, inspected 2026-10-08.
Audience: implementation agents, including GPT 6 Luna, and human reviewers.
Product source: the accompanying design conversation. All MUST requirements below
are implementation acceptance criteria, not assertions about today's product.

Start with [the product contract](#2-problem-and-product-contract), then read
[parameter semantics](#4-parameter-and-calendar-contract),
[lexical rules](#5-scanner-and-renderer-exact-behavior), and
[durable execution](#7-execution-architecture-and-durable-artifacts).
The [UX requirements](#9-new-job-ux-specified-interactions),
[test matrix](#14-required-adversarial-test-matrix), and
[work packages](#17-ordered-implementation-work-packages) are normative too.

## 1. How an implementation agent must use this document

1. Read this document completely before modifying code. Read repository
   `AGENTS.md`, `CONTRIBUTING.md`, and the applicable skill/reference files.
2. Implement one work package from section 17 at a time, in dependency order.
   Do not replace this design with a simpler regex, guessed SQL rewrite, generic
   scheduling feature, or new execution engine.
3. Inspect current code and tests at each named seam. The baseline is a map, not
   permission to overwrite later changes. Record material drift before coding.
4. Preserve already-working source/destination, capacity, authentication,
   cancellation, queue, CSV, and detached-runner behavior.
5. Write the specified behavior tests with each package. Tests must check
   observable contracts and failures, not merely repeat implementation steps.
6. Do not silently resolve a contradiction by weakening an invariant. Follow
   section 19. Routine internal naming and function decomposition are discretionary.
7. Update the implementation checklist in the implementing PR, with files,
   commands/results, remaining gaps, and the next package. Do not mark mock
   evidence as production evidence.
8. Follow the repository PR workflow. This specification authorizes design work;
   future implementation must be requested. It does not authorize production
   execution, deployment, merging, or release operations.

## 2. Problem and product contract

Today the user selects MonthlyJob, enters full dates, and embeds literal
`{date_inicio}` and `{date_fim}` tokens in SQL. The orchestrator expands the
selected dates to whole months, replaces all literal occurrences, creates monthly
tables, and concatenates their rows. The interface does not adequately expose
these semantics. Complex queries can legitimately use dates repeatedly in distant
CTEs and can need different time scopes in different parts of the same query.

The new contract is:

> Run the author's query once per selected calendar month. Supply that slice's
> dates and the entire requested range's dates at explicit parameter references.
> Combine the returned rows with UNION ALL, preserving duplicates.

The author owns predicate placement, joins, lookbacks, business time conventions,
and the meaning/granularity of returned rows. Dispatch owns calendar generation,
typed parameter rendering, reference navigation, reviewed snapshots, and execution.

Repeated parameters in complex SQL are supported, not a special-case workaround.
Dispatch MUST NOT infer that a query is semantically equivalent to one unsliced
execution just because rendering, parsing, or execution succeeds.

### 2.1 Scope locked for v1

Included:

- Calendar-month selection; an inclusive first and last month.
- Four date-valued parameters, explicitly placed by the author.
- Whole-file discovery, precise reference navigation, slice switching, boundary
  review, a safe filter-snippet generator, and contextual findings.
- One query expression per template, including CTEs and nested subqueries.
- A reviewed immutable plan used by TUI, CLI, and notebook launch paths.
- Existing single-Impala-session monthly execution and UNION ALL combination.
- Legacy templates and historical manifests retaining legacy behavior.
- Read-only diagnostics and explicit review; no automatic edits to SQL files.

Deferred; do not implement incidentally:

- Partial first/last months or arbitrary split intervals.
- Recurring schedules, cron, automatic reruns, independent slice retries,
  parallel slice execution, or per-slice resume/checkpoints.
- A new SQL editor, database-schema browser, automatic predicate placement,
  arbitrary user-defined template parameters, or a second expression language.
- Automatic final aggregation/deduplication, atomic output publication, snapshot
  isolation across source tables, or output-schema coercion.
- Execute-selected-slice, EXPLAIN, or other engine calls from the preview.
- Automated legacy conversion. V1 supplies reference inspection and migration
  guidance; a conversion tool needs a separate operator-aware design.

### 2.2 Success criteria

- A user with an existing template can select months and launch without entering
  or configuring individual parameter values.
- A user can locate every executable reference, even beyond the first 8 KiB.
- Moving between slices visibly changes slice values and preserves range values.
- Definite configuration mistakes block all launch interfaces consistently.
- The acknowledged source and rendered script are the ones executed.
- A legacy job continues to produce its previous SQL and orchestrator behavior.

## 3. Repository facts and ownership

| Concern | Existing owner / baseline fact |
|---|---|
| Form, preview, launch confirmation | `dispatch/screens/new_job.py`; currently reads SQL in several paths and picker detection examines only an initial excerpt |
| Preview / styling | `dispatch/screens/preview.py`, `dispatch/app.tcss` |
| Shared launch | `dispatch/job_ops.py`: `LaunchInputs`, `LaunchPlan`, `prepare_launch`, sync/async execution |
| Legacy SQL helpers | `dispatch/sql.py`: detection, date conversion, monthly preview |
| Durable job and argv | `dispatch/manifest.py`: schema version 1, `create_job`, `build_orchestrator_calls` |
| Capacity | `dispatch/jobs.py`: sync/async creation under existing admission machinery |
| Runner | `dispatch/runner.py`: durable subprocess ownership and per-call environment |
| Monthly orchestrator | `scr/monthly_query_processor.py`: one combined script, one call to retry machinery |
| CLI / notebook | `dispatch/cli_job.py`; `dispatch/notebook.py` wraps CLI subprocesses |
| Clone/prefill | `dispatch/screens/job_detail.py`, `new_job.py` |
| Advisor | `dispatch/advisor/`; SQLGlot 30.12.0; static-only, flag-only, parser output never executes |
| Monthly destination | `SqlTemplate -> Table` only; internal source enum remains `SqlTemplate` |
| Output | `<schema>.<base>_fulljoin`; monthly temps `<base>_temp_YYYYMM`; UNION ALL despite the historical suffix |
| Storage | Existing schema-prefix HDFS path and user/EID naming rules |

Introduce `dispatch/monthly.py` as the owner of the new immutable plan, scanner,
calendar generation, renderer, reference model, and new-format script assembly.
Keep it free of Textual, filesystem mutation, subprocesses, and network calls.
Split into focused modules only when needed for readability; retain one public
domain API and one renderer. Do not invent a generic templating framework.

Add `dispatch/screens/monthly_preview.py` for the interactive monthly preview.
Reuse existing syntax rendering and shared chrome. Keep ordinary preview behavior.
An optional `scr/_monthly_plan.py` may contain the stdlib-only artifact reader;
it MUST NOT contain another template scanner or parameter renderer.

## 4. Parameter and calendar contract

### 4.1 Names and meanings

| Exact token | Date value | Variation |
|---|---|---|
| `:slice_start` | First day of current month, inclusive | Changes each slice |
| `:slice_end` | First day of following month, exclusive | Changes each slice |
| `:range_start` | First day of first selected month, inclusive | Same for all slices |
| `:range_end` | First day after last selected month, exclusive | Same for all slices |

Names are case-sensitive. Tokens are unquoted SQL expressions. Render each as
`CAST('YYYY-MM-DD' AS DATE)`; never as an unquoted date or a raw user-provided SQL
value. This exact representation is conditional on the release compatibility gate
in section 18; do not substitute a different type silently.

For January through March 2026:

| Slice | slice_start | slice_end | range_start | range_end |
|---|---|---|---|---|
| 2026-01 | 2026-01-01 | 2026-02-01 | 2026-01-01 | 2026-04-01 |
| 2026-02 | 2026-02-01 | 2026-03-01 | 2026-01-01 | 2026-04-01 |
| 2026-03 | 2026-03-01 | 2026-04-01 | 2026-01-01 | 2026-04-01 |

Date values are calendar dates, with no implicit conversion of timestamp columns
or timezone. DATE-to-TIMESTAMP comparison follows the supported Impala engine's
behavior. Authors own timezone normalization and string/numeric partition casts.
Dispatch does not rewrite a partition column's type or apply a function to it.

### 4.2 Inputs and validation

- V2 month text has exact ASCII format `[0-9]{4}-(0[1-9]|1[0-2])` using fullmatch.
- Validate the date domain after matching. Year 0000 is invalid.
- Last month must be on or after first month. One month is valid.
- Reject a last month whose exclusive end is unrepresentable (9999-12).
- Generate month starts with year/month arithmetic, not 30-day durations.
- Selection defaults to the launch host's current calendar month, matching the
  existing application convention. Resolve it once when constructing the form;
  a midnight/month rollover must not change an open form underneath the user.
- Generation has no arbitrary product limit on month count. Before materializing
  script bytes, calculate projected output size and enforce resource limits below.
- Slice IDs are `YYYY-MM`, stable within the plan and ordered chronologically.

### 4.3 Valid query uses

Parameters may occur in filters, joins, CASE expressions, date arithmetic,
projections, nested SELECTs, or any other legal expression position. Dispatch
validates lexical references; the engine determines expression/type validity.

At least one executable slice reference is required for monthly v2 mode. Requiring
both boundaries would incorrectly reject valid as-of or derived-boundary queries.
If only one slice boundary is used, emit an informational finding. A slice
parameter used solely in a projection satisfies this rule; do not falsely claim
it proves rows are restricted to that slice.

Range-only templates cannot use monthly v2 execution. Offer an explicit Run once
action; that action retains the requested month interval and renders range
parameters once (section 11). Never launch automatically or blindly remove tokens.

## 5. Scanner and renderer: exact behavior

### 5.1 Supported lexical contract

Scan the original Unicode source in a single forward pass. Maintain code, single
quoted string, double quoted string, backtick identifier, line comment, and block
comment states. Support doubled delimiters and backslash escapes according to the
repository's Impala lexical corpus. Line comments end at LF or CRLF. Block comments
end at the first closing `*/`; nested block comments are unsupported and must
produce a lexical error when an inner `/*` occurs inside a block comment.
Unterminated strings, quoted identifiers, or block comments block v2 launch.

Document the supported escape behavior with examples and tests before using it.
Do not assume the existing terminal-semicolon scanner covers backtick identifiers.

In executable code a parameter candidate is `:` followed by an ASCII identifier
`[A-Za-z_][A-Za-z0-9_]*`. Require a token boundary before `:`: preceding character
must not be an identifier character or another colon. `::` cast syntax is not a
Dispatch parameter; leave it to SQL validation. Consume the complete identifier;
`:slice_start_extra` must never be partially replaced.

In an explicitly selected v2 template, all executable parameter candidates outside
the four-name allowlist are blocking unknown-parameter findings. This deliberately
does not support third-party bind variables in v2. Ordinary SqlFile queries are
not subjected to this unknown-bind-variable rule.

Do not classify a file as v2 solely because a random unknown `:name` appears. Known
executable tokens, known quoted tokens, or explicit v2 selection activate v2
diagnostics. A user with only misspelled names can select v2 explicitly and see
the errors.

### 5.2 Comments, literals, legacy mixtures

- New-format tokens in comments are ignored, preserved, and excluded from counts.
- A complete known token inside a string or quoted identifier is a blocking
  `M004_QUOTED_PARAMETER` in v2. Preserve text and point to its exact location.
  Explain that v2 reserves these spellings even as literal text; an intentional
  literal of that form must use ordinary SQL mode or be expressed differently.
- Ordinary SQL with quoted parameter-looking strings must remain executable as
  ordinary SQL. File classification can suggest v2, never force a mode switch.
- Legacy token spellings in strings or executable code plus new-format executable
  or quoted tokens block v2 as mixed format. Legacy spellings only in comments do
  not count toward a v2 mixture. Legacy mode retains its previous global-replace
  semantics, including comments; do not "fix" it incidentally.
- A legacy template containing executable v2 tokens must not launch with those
  tokens unresolved. Show the mixed-format error. Literal new-format-looking
  text inside legacy strings is preserved as before and may be informational.

### 5.3 Source spans and replacement

Record token name, occurrence ID, half-open character offsets, one-based source
line/column, and context classification. Character offsets refer to the decoded
source; artifact hashes refer to explicitly encoded UTF-8 bytes. CRLF and non-ASCII
text before a token must not corrupt line mapping.

Render by joining untouched source segments and generated expressions using the
recorded spans. Never render an AST back to SQL. Every non-replaced source byte
must round-trip under the chosen UTF-8 encoding. Use a source-to-rendered span map
for preview highlights and findings. Generated wrapper spans have no source line.

Remove at most one terminal statement semicolon, respecting comments and quotes,
before placing the query in CTAS. Ensure a newline before wrapper delimiters so a
trailing line comment cannot consume them. Reject multiple executable statements.
An optional trailing delimiter and trailing comments are allowed. Permit a first
executable keyword of SELECT or WITH; reject obvious non-query/DDL leaders.
Do not block an otherwise lexically supported SELECT solely because SQLGlot
cannot parse valid Impala syntax. Report structural analysis unavailable.

### 5.4 Resource limits

V2 source limit: 5 MiB of UTF-8 bytes. Generated full-script limit: 64 MiB.
The artifact reader must enforce the same script limit and a 16 MiB plan-JSON
limit before unbounded reads. Exceeding a limit is an explicit configuration error
with the measured/projected size and a suggestion to reduce the period or query.
Do not truncate source, reference discovery, or the executed script.

Scan once per source revision. Build rendered SQL off the UI event loop. Preview
only the selected slice or a bounded script window; do not render thousands of
SQL lines into widgets simultaneously. These limits are v2 implementation bounds,
not retroactive limits on legacy jobs.

Calculate generated UTF-8 size from untouched segments, substituted expressions,
and wrapper/UNION ALL fragments before allocating all rendered slices. If that
calculation is streamed, stop at the limit rather than completing an oversized
buffer. UI performance tests use a heartbeat/progress assertion under slow mocked
analysis, not a flaky machine-specific millisecond threshold.

## 6. Domain API and data model

Use frozen dataclasses and tuples for in-memory immutable data. The names below
are the required public conceptual API; minor signature adaptations to existing
typing conventions are allowed if these inputs/outputs and invariants remain.

```python
scan_template(source: str) -> TemplateScan
build_period(first_month: str, last_month: str) -> Period
render_slice(scan: TemplateScan, period: Period, slice_id: str) -> RenderedQuery
build_monthly_plan(scan, period, destination, user) -> MonthlyPlan
build_range_once_plan(scan, period, destination, user) -> PreparedRangeQuery
```

`TemplateScan`: source string/hash; lexical format; tuple of references; tuple of
diagnostics; tuple of executable statement spans. `Period`: input months;
range dates; ordered immutable Slice records. `RenderedQuery`: query text;
parameter values; original/rendered source map. `MonthlyPlan`: version; source
hash; period; references; findings; rendered slices; full execution script;
script hash; expected temporary table list; effective final table; renderer version.

Diagnostics carry stable code, severity (`error`, `warning`, `info`), summary,
optional source span, and a concrete next action. Severity `error` in this domain
means a non-overridable template/plan error. Do not reuse Advisor's overridable
error category for malformed templates.

Extend `LaunchInputs` additively with optional `template_format`, `first_month`,
`last_month`, and `execution_mode` fields. Existing call sites remain valid.
Allowed formats: `legacy-v1`, `monthly-v2`; omission follows section 11.
Allowed explicit modes: `once`, `monthly`. Add an optional prepared monthly/range
plan to `LaunchPlan`; widen overly narrow `params` typing deliberately where needed.
Prepared SQL must flow through both sync and async capacity/create paths.

## 7. Execution architecture and durable artifacts

### 7.1 Chosen architecture

Compile new-format monthly SQL in `dispatch/monthly.py` before launch. Persist the
reviewed complete script and a versioned plan sidecar. The existing monthly
orchestrator reads those artifacts for v2 and passes the script to its existing
`execute_step_with_retry` once. It does not re-render the template.

This avoids requiring Textual/SQLGlot/Dispatch imports inside `scr/`, keeps one
v2 renderer, and preserves the single-coordinator execution fix. Legacy template
rendering remains in the existing legacy code path.

The new builder reproduces existing wrapper rules: validated schema/base/user,
schema-prefix HDFS path, monthly temp names, final `_fulljoin` destination,
ordered UNION ALL, and cleanup statements after final-table creation. Do not add
metadata invalidation, coordinator switching, email changes, altered retries,
different pools, or new output naming in this feature.

### 7.2 Artifact layout for v2 monthly jobs

Inside the existing private job directory:

```text
manifest.json          existing manifest, schema_version 1
job.sql                original reviewed v2 template, UTF-8
monthly-plan.json      versioned period/output/hash metadata
monthly-execution.sql  exact reviewed complete executable script, UTF-8
run.log                existing job log
```

Use additive manifest params:

```json
{
  "template_format": "monthly-v2",
  "execution_mode": "monthly",
  "first_month": "2026-01",
  "last_month": "2026-03",
  "start_date": "01/01/2026",
  "end_date": "03/31/2026",
  "monthly_plan_version": 1,
  "monthly_plan_file": "monthly-plan.json",
  "monthly_plan_sha256": "<sha256 of stored JSON bytes>",
  "source_sha256": "<sha256 of job.sql bytes>",
  "execution_sha256": "<sha256 of monthly-execution.sql bytes>"
}
```

Existing email/subject/queue params remain. Dates in old argv are derived from
months for compatibility, not accepted as a second competing input for v2.
Manifest `source.type` remains `SqlTemplate`; destination remains Table with its
existing base-name convention. Persist `effective_final_table` in plan metadata
and expose it in launch summaries, Job show, and detail. Do not globally reinterpret
the base destination field in old manifests.

`monthly-plan.json` version 1 contains exactly: plan_version, renderer_version,
template_format, source_sha256, execution_sha256, first_month, last_month,
range_start, range_end, ordered slices (id/start/end), schema, base_table, user,
ordered temporary_tables, effective_final_table. It contains no SQL body or
arbitrary paths. Date strings in this JSON use ISO dates. Serialize deterministically
as UTF-8 JSON with sorted object keys and a documented final-newline policy.

Write artifact files completely before publishing the Pending manifest; use the
existing private-directory and atomic-write conventions. A disk error must not
produce a discoverable Pending job or hand off a runner. Test cleanup/retention
of an unpublished job directory and capacity release. Do not hold the capacity
lock during SQL analysis/rendering; keep publication within existing admission.

### 7.3 Runner/orchestrator handoff

Runner sets `DISPATCH_MONTHLY_PLAN_PATH` to this job's fixed `monthly-plan.json`
only for a manifest explicitly carrying monthly-v2 monthly execution. It sets
`DISPATCH_MONTHLY_PLAN_SHA256` from the stored manifest hash. Remove inherited
values for both keys for every other orchestrator call. Never trust shell/global
environment selection of a plan for a legacy job.

Before starting a v2 call, runner verifies artifacts and fails the job if missing
or inconsistent. The stdlib sidecar reader repeats hash/schema checks immediately
before returning loaded script text. Read each artifact once, hash the exact bytes
that will be decoded/executed, and do not reopen after validation.

Reader requirements: fixed basenames relative to the job.sql parent; reject
symlinks/nonregular files and unsupported versions; enforce limits; verify JSON
hash, source/script hashes, and consistency of date interval, slice sequence,
table list, schema/base/user with existing argv. Require the path supplied by
runner to be exactly the job-scoped fixed path. No fallback to legacy behavior
on a missing/corrupt/unsupported v2 plan. These checks detect corruption and
misrouting, not malicious tampering by the same operating-system user.

If a v2 snapshot reaches a legacy script without the environment sidecar, it must
fail before issuing SQL because legacy required tokens are absent. Add a regression
test for this fail-closed downgrade. Explicitly test installed-version skew; do
not assume working-tree tests prove the installed scr bundle supports v2.

No new public flags for `monthly_query_processor.py`. Preserve its args, exit
behavior, plan/start/finish/failure email behavior, retry function, and monitoring
seam. The new branch changes only where its script/table plan comes from.

### 7.4 Policy prerequisite

ADR-0005 does not currently list this sidecar input as an allowed change category.
Package P0 must propose a narrow ADR amendment for versioned precompiled monthly
plans, explicitly preserving argv, queues, retries, email, coordinator ownership,
and legacy outputs. Do not claim the existing observability exception authorizes
this execution input. The amendment and runtime change require normal review;
scr PRs retain the `[scr/]` label, all-scenario evidence, and two-reviewer process.
No production operation is necessary to write or review the implementation locally.

## 8. Snapshot lifecycle and stale-result handling

Model New Job preparation as `empty -> loading -> invalid|ready -> reviewing ->
launching`. Any source, period, mode, output, or relevant launch-input edit bumps
a monotonically increasing generation ID and invalidates dependent preparation.

Read selected SQL off the event loop as UTF-8, preserving newline bytes (do not
use universal-newline translation for hashing). Invalid UTF-8 is actionable failure,
not `errors="replace"`. An optional leading UTF-8 BOM may be accepted and preserved
in source while excluded from executable lexical prefix; test it explicitly.

All discovery, reference counts, findings, previews, and final scripts belong to
one prepared generation. Discard worker results whose generation no longer matches
or whose screen has unmounted. Cancel/coalesce obsolete work. Do not key caches
only by pathname; include content hash and all relevant plan inputs.

On opening final confirmation, re-read/hash the source and compare with the
prepared source. If changed, rebuild and show "Query changed; review the updated
plan." Do not accept an earlier acknowledgement for the rebuilt plan. Existing
confirmation must consume a LaunchPlan, not independently reconstruct a summary.

At confirmation acceptance, recheck source hash once. If it changed during review,
return to updated review. After acceptance the snapshot is frozen: changes made
while waiting for capacity cannot alter execution. Do not poll the original file
forever or restart a prepared launch because it changes after acceptance. This is
the intentional cutoff, and the confirmation says "Runs the reviewed snapshot."

CLI prepares and reports a single frozen snapshot per invocation; --yes accepts
that snapshot. Notebook delegates to this path. Recheck authentication/admission
as today without rebuilding SQL after acceptance.

Cloning a v2 job restores format, mode, selected months, source template, and output
inputs. Rebuild a fresh plan for the new job; never reuse old script paths/hashes
after changing inputs. Historical inspection must use stored job artifacts even
when the original source has moved or been deleted.

## 9. New Job UX: specified interactions

### 9.1 Primary form

Present Query file versus Existing table as source intent. For Query file show
Execution: Run once / Run for each month. Internally map monthly to SqlTemplate
and ordinary once to SqlFile. Preserve ExistingTable -> Csv and existing ordinary
SqlFile destination choices. Monthly remains Table-only; explain the restriction.

For new v2 monthly show First month / Last month controls. Use keyboard-editable
YYYY-MM input with month labels/helper text; do not require a graphical calendar.
Default both to the current month. Do not show four independent date inputs.

```text
Query file    customer_activity.sql
Execution     Run for each month
Months        2026-01 through 2026-03    3 slices
Parameters    8 references found        [Review parameters]
Result        Combine rows; duplicates preserved
              aa_enc.e123456_activity_fulljoin
```

Source selection never silently changes a user-chosen execution mode. Known v2
tokens may show "This query has date parameters" with an explicit configure
action. Legacy templates get a visible Legacy monthly template label and existing
date controls/semantics. A format selector in advanced query settings allows
explicit v2 selection to diagnose unknown-only tokens. No format auto-downgrade.

Switching from monthly to once while executable slice tokens remain must show
an actionable error; it must not send unresolved tokens to Impala. Switching modes
preserves entered period values so users can switch back without losing work.

### 9.2 Progressive disclosure

Default parameter details to collapsed. Show only used parameters; unused range
parameters do not create controls, warnings, or required configuration.
Header counts executable occurrences only. Quoted invalid occurrences appear in
the error list separately. Display values read-only: users configure months, not
generated variables. Errors remain visible even when details are collapsed.

### 9.3 Monthly preview screen

Offer views: Overview, References, Slice SQL, Boundary, Execution script. These
may use tabs or a compact selector with the same keyboard-accessible labels.

- Overview: requested months, slice count, effective destination, UNION ALL
  behavior, inclusive/exclusive explanation, and per-slice range rows.
- A slice selector has previous/next controls, a YYYY-MM selection/jump, and
  "Slice 2 of 3". Do not allocate one horizontal button per month.
- Parameter table: used names, concrete ISO values, "Changes each slice" or
  "Same for all slices". Highlight changes with a text marker as well as color.
- References: stable occurrence IDs in source order; name, line/column, and
  bounded SQL excerpt. Optional CTE names only when structurally established.
  Never invent a CTE/table association when parsing is unavailable.
- Selecting a reference opens the source context with exact span and current
  value, with next/previous occurrence actions. Navigates anywhere in the file.
- Slice SQL: the rendered query for the selected month, source-aware highlights,
  and existing copy behavior. An explicit label distinguishes it from the entire
  execution script.
- Execution script: reviewed wrapper/UNION ALL/cleanup SQL, paged/windowed; copy
  may include the full script but must not pretend clipboard success on failure.
- Copy failure: preserve selection and show a visible/selectable text fallback.
- Excerpts and filenames are escaped for Rich/Textual markup. SQL is never markup.

Reference lists can page 100 rows at a time; show total matches and current page.
Support name filtering and line jump without discarding undisplayed references.
Use a bounded 200-line context window with an explicit range label; permit
navigation to adjacent windows. Discovery/counts must cover the entire source.

### 9.4 Boundary view

Select an adjacent pair of slices. Show last included calendar date and exclusive
boundary for the left slice, and inclusive start for the right. For January and
February show January end < 2026-02-01 and February start >= 2026-02-01.
The same source reference context appears for both slices with substituted values.
At 120x40 use side-by-side panes if legible; at 80x24 use stacked/selectable panes.
For a one-slice range show "No adjacent slice in this run" and the single interval.

Copy must say this displays generated boundaries; query predicates determine row
membership. Do not show "No gaps or duplicates" as a inferred assurance.

### 9.5 Filter-snippet helper

Action label: Build date filter. It opens a small helper; default scope Slice,
alternate Range with helper "Same requested period in every execution".
Request a single-line SQL date expression. Reject blank input and executable
statement delimiters/comments; validate balanced supported quoting/parentheses.
Do not restrict valid qualified/cast expressions to a bare identifier. This helper
is advisory and does not execute the expression or assert its SQL type.

Output, with the expression parenthesized on each occurrence:

```sql
(p.purchased_at) >= :slice_start
AND (p.purchased_at) < :slice_end
```

Use range names for Range scope. Show Copy snippet and Reload query actions.
The helper does not add WHERE/AND to the file or choose an insertion point.
Copy status must be honest over SSH; provide a selectable snippet on failure.
The user edits the file using their normal editor, then reloads. Blank placeholder
expressions are instructional only and cannot be copied as a supposedly valid filter.

### 9.6 Accessibility and responsiveness

Preserve q, ?, F2 and applicable Back bindings. All new actions are reachable by
Tab/Shift+Tab and activated with keyboard controls; scoped shortcuts cannot capture
text while an input is focused. Keep visible focus and text severity labels.
At 80x24 keep errors, current slice/period, and primary actions reachable without
horizontal scrolling. Collapse secondary content, not launch/error information.
Resize preserves selected slice, reference, source window, and mode. Test NO_COLOR.
Move file reads, full scans, hashing, rendering, and Advisor work off the event loop.
No database calls from composition, validation, snippet, or preview actions.

## 10. Diagnostics and mistake prevention

### 10.1 Stable error codes

| Code | Trigger | Required action/message |
|---|---|---|
| M001_INVALID_MONTH | Bad format/domain or reversed period | Identify field; use YYYY-MM; first <= last |
| M002_UNKNOWN_PARAMETER | Unknown executable candidate in v2 | Exact spelling, source location, supported names |
| M003_MIXED_FORMAT | Mixed new/legacy executable configuration | Choose one format; manual migration needed |
| M004_QUOTED_PARAMETER | Reserved v2 name inside quotes | Remove quotes; Dispatch provides a date expression |
| M005_NO_SLICE_REFERENCE | Monthly v2 has no executable slice reference | Explain unchanged query; offer Run once when range references exist |
| M006_LEXICAL_ERROR | Unterminated/unsupported lexical construct | Exact location; no guessed repair |
| M007_QUERY_SHAPE | Multiple statements or obvious non-query leader | One SELECT/CTE query per template |
| M008_RESOURCE_LIMIT | Source/script/plan limit exceeded | Size and concrete reduce-period/query action |
| M009_PLAN_MISMATCH | Missing/corrupt/inconsistent artifact | Refuse execution; re-create job from source |
| M010_MODE_CONFLICT | Slice references in once; incompatible input flags | Identify incompatible mode/fields |

All M errors block regardless of --yes or --acknowledge-advisor. Return all
available independent form/lexical errors in stable field/source order; do not
hide deeper references merely because the first one is wrong. Lexical corruption
may stop reliable scanning; explicitly mark further diagnostics incomplete.

### 10.2 Nonblocking findings

- W001_INCLUSIVE_END: when reliable structural analysis establishes a BETWEEN
  upper bound or <= comparison directly using slice_end or range_end, explain that
  it includes the first instant/day of the next interval. Show the predicate and
  a suggested >= / < pattern. No automatic file modification.
- I001_SINGLE_SLICE_BOUNDARY: only one of the slice boundary names is used. Say
  this may be intentional; ask the author to inspect the predicate.
- I002_GLOBAL_CONTEXT: always-present output explanation that joins/windows/
  aggregates operate within each execution, and UNION ALL does not recompute them.
- I003_ANALYSIS_UNAVAILABLE: structural SQL analysis unavailable; lexical parameter
  checks and reference navigation still work.

No warning simply for mixing range and slice scopes. No broad cross-month-join or
double-counting detector in v1: the application cannot reliably infer intent.
Do not infer row filtering from token occurrence counts. Existing Advisor rules
retain their established severities and acknowledgement behavior.

### 10.3 Advisor integration

V2 introduces an explicit amendment to the Advisor's legacy template-only parse
contract. Analyze the selected slice's rendered query for interactive context;
analyze the first slice for the launch's standard static Advisor report and label
that scope. Existing Advisor rules inspecting the requested monthly interval must
receive the requested period explicitly, rather than mistakenly seeing only one
month. Audit R04 and its existing source applicability before adapting it.

Map actionable query findings back to original spans through the renderer's map;
unmapped findings remain query-level, not fabricated line numbers. A parse copy
may replace parameters with safe expressions and maintain a map, but parser-
generated SQL must never enter executable artifacts. No duplicated Advisor gate:
the existing final confirmation/gate includes the new period/output summary.
Update `docs/query-optimization-advisor-spec.md` narrowly for this v2 extension.
Legacy Advisor behavior and ordinary query behavior remain unchanged.

## 11. CLI, notebook, and once-mode parity

### 11.1 Monthly CLI

Keep existing required --source and --destination. Add:

```text
--template-format {legacy-v1,monthly-v2}
--first-month YYYY-MM
--last-month YYYY-MM
--execution-mode {once,monthly}
```

Monthly v2 example:

```sh
dispatch job launch --source SqlTemplate --destination Table \
  --template-format monthly-v2 --execution-mode monthly \
  --sql activity.sql --schema aa_enc --table activity \
  --first-month 2026-01 --last-month 2026-03 --yes
```

- Without new flags, old invocations remain legacy-v1 for SqlTemplate. Do not
  silently reinterpret dates or detect/upgrade a CLI call from file contents.
- Explicit v2 requires both month flags; missing mode derives monthly for
  SqlTemplate or once for SqlFile. Explicit contradictory source/mode is M010.
- V2 rejects --start-date/--end-date; legacy rejects month flags.
- Monthly v2 rejects Csv and Table+Csv through the existing legal-cell machinery.
- Preserve exit codes 0 success, 2 usage/configuration, 4 operational refusal.
- Human summary shows period, slice count, effective output, duplicates preserved,
  source hash abbreviation, and snapshot behavior. JSON remains valid JSON without
  human status lines; expose machine-readable format/mode/period/diagnostics.
- --yes never launches SQL containing template errors.

### 11.2 Range-only Run once

Offer Run once only as an explicit choice. Retain month selectors while executable
range tokens remain. Supply range_start/range_end once; reject all executable slice
references. Map to SqlFile with prepared rendered SQL and existing normal destination
choices. Keep the original source in a separate `template.sql` snapshot and store
format/months/source hash/rendered hash in params; job.sql remains executable SQL
under existing ordinary wrapping rules. Snapshot review/clone uses template.sql.

CLI uses --source SqlFile --template-format monthly-v2 --execution-mode once plus
the month flags. This format name identifies the parameter language, not the
execution count. Only range parameters are legal in this path. Do not emulate Run
once by setting slice_start/range_start equal, silently run one monthly CTAS, or
leave parameters unresolved. Parameter-free once jobs continue through the old path.

### 11.3 Notebook

Extend both query and file-launch API surfaces with optional template_format,
execution_mode, first_month, last_month keyword arguments. Forward to the CLI;
do not import TUI/shared launcher internals or reproduce date logic. Legacy
start_date/end_date API behavior remains. Preserve eager submission and strict
result-read contracts in ADR-0008/0009/0010. Argument conflicts must surface before
job submission. Update API docs with one monthly and one range-once example.

For range-once artifacts, distinguish the hash of the source template, rendered
query, and final executable job.sql after the existing destination wrapper. Test
all three against their own bytes; never compare a pre-wrapper hash to job.sql.
Use the shared existing wrapper exactly once. Explicit v2 once mode with no range
references is M010 with guidance to use ordinary Run once; this prevents unused
period settings from implying a date filter that does not exist.

## 12. Legacy, history, and migration

Legacy absence of template_format means legacy-v1, including historical manifests.
Do not require v2 artifacts to inspect/run/clone old jobs. Unknown explicit future
versions must fail launch clearly but remain inspectable as unsupported history.

Legacy end values are inclusive calendar month ends; new end values are exclusive
first-of-next-month DATE expressions. Do not bulk-replace date_fim with slice_end.
Legacy day precision accepted by strptime is unchanged; v2 month inputs are strict.

Provide an in-product migration explanation with a before/after filter example,
quoted/unquoted distinction, references to review, and instructions to save a
new file. Explain that BETWEEN/end projections/date arithmetic need individual
review. No automatic conversion or acknowledgement that declares migration safe.

Existing jobs keep their source/destination display compatibility in history;
new job details add execution/format labels rather than rewriting enums. Effective
final-table summaries must be correct for legacy monthly jobs too when derivable.

## 13. Execution limitations shown honestly

- UNION ALL preserves duplicate rows and does not recompute global aggregates.
- Same-destination jobs may collide under the existing naming/overwrite rules;
  this change does not introduce atomic publication or destination locking.
- Failure during a single script can leave temporary tables or an incomplete/
  missing final output under existing behavior. Do not mark planned slice rows
  successful just because their script text exists.
- No per-slice progress unless backed by existing reliable telemetry. V1 may show
  planned slices alongside the overall Pending/Running/Succeeded/Failed/Cancelled.
- Separate statements can observe changing source data; same session is not a
  promise of a database-wide snapshot.
- Query shapes/types that differ by month may fail UNION ALL. Preserve the engine
  error; do not reorder/cast/drop columns to make it pass.

Put concise output semantics near launch. Avoid a mandatory generic warning
checklist or additional modal for every informational finding.

## 14. Required adversarial test matrix

Each row is a required observable assertion, not a suggested test name. Fixtures
must include a real large CTE query, not just repeated replacements in SELECT 1.

| ID | Case | Expected result |
|---|---|---|
| C01 | Jan–Mar / one month / Dec–Jan | Correct ordered slices; exclusive ends; one-month still monthly |
| C02 | Feb 2024 and Feb 2026 | Correct leap and non-leap boundaries |
| C03 | 2026-1, Unicode digits, month 00/13, year 0000, reversed, 9999-12 | M001; no crash or silent normalization |
| C04 | End at representable domain edge | No overflow during generation |
| L01 | Repeated tokens in early and late CTEs beyond 8 KiB | Every occurrence discovered and rendered |
| L02 | Similar name :slice_start_extra / wrong case | M002; no partial replacement |
| L03 | Tokens in line/block comments | Preserved, excluded from counts |
| L04 | Known names in quoted string/backtick identifier | M004 in v2; ordinary SQL remains available |
| L05 | Doubled quotes, backslash escapes, embedded semicolons/comments | Correct lexical states; source unchanged elsewhere |
| L06 | CRLF, Unicode prefixes, BOM, final line without newline | Correct source lines/spans/hashes |
| L07 | Unterminated/nested unsupported constructs | M006; incomplete scan status; no launch |
| L08 | Multiple executable statements / DDL / WITH query | Reject first two; support CTE query |
| L09 | Trailing semicolon plus --comment | Wrapper remains executable; comment does not consume delimiter |
| L10 | Params in projections, CASE, date arithmetic | Accepted lexical uses without false filtering claims |
| L11 | Legacy tokens plus new tokens | Correct mixed-format behavior per section 5 |
| L12 | V2 typo-only file explicitly selected | M002, not a forced legacy fallback |
| R01 | All four parameters across months | Slice changes; range stays constant; DATE expressions unquoted by author |
| R02 | Range-only monthly | M005; explicit Run once produces one prepared query |
| R03 | Slice tokens in once | M010; no unresolved SQL sent |
| R04 | One slice token / projection only | Accepted; informative limitation, not semantic assurance |
| R05 | Source/script above limits | Bounded rejection before launch/materialization |
| S01 | January purchase, February refund | Mixed-scope fixture can retain cross-month match; no filter relocation |
| S02 | Both inputs slice-filtered | Preserve author SQL; no claim all cross-month matches retained |
| S03 | Lookback overlaps slices | No implicit dedup; output ownership remains authored |
| S04 | Monthly AVG/distinct/window/rank | No inferred globally equivalent aggregation |
| S05 | BETWEEN/<= direct end parameter | W001 with mapped span when analysis succeeds |
| S06 | Parser unsupported syntax, lexer supported | References/rendering work; analysis unavailable, no fake spans |
| S07 | Duplicate rows and incompatible schemas | UNION ALL keeps duplicates; incompatible schemas surface engine failure |
| P01 | Edit file before/during confirmation | Updated plan requires new review; old acknowledgements invalid |
| P02 | Edit after acceptance / while capacity waits | Executes frozen accepted snapshot |
| P03 | Old worker completes after new input / screen close | Result ignored; widgets untouched |
| P04 | Missing/corrupt plan/source/script/hash/version | M009/failure; zero Impala invocations |
| P05 | Interrupted artifact write / disk full | No published Pending job or leaked admission slot |
| P06 | Symlink/outside path/wrong argv period or table | Reject plan before SQL execution |
| P07 | Inherited plan env on legacy/unrelated call | Removed; old behavior preserved |
| P08 | V2 artifacts, older orchestrator/no sidecar | Fail before SQL; no legacy reinterpretation |
| P09 | Clone/history after original removed | Use stored original template; clone builds fresh artifacts |
| E01 | Happy monthly run | One retry entry point / one script per attempt; temp+union+cleanup same session |
| E02 | All existing mock failure scenarios | Legacy behavior equivalent; v2 lifecycle/classification preserved |
| E03 | Cancellation and capacity | Existing process-group ownership and two-slot invariants preserved |
| U01 | Only slice params | Range controls absent by default; no mandatory range explanation |
| U02 | Slice switch and reference selection | Values update, selection stable, exact source context visible |
| U03 | 100+ references and large SQL | Full counts; bounded navigation; no hidden truncation |
| U04 | 80x24, 120x40, resize, keyboard, NO_COLOR | All primary actions reachable; no color-only meaning |
| U05 | Clipboard unavailable / source unreadable | Honest visible error and fallback; no false success |
| U06 | Source/mode switching, invalid field correction | Values preserved; stale plan disabled; errors clear when corrected |
| U07 | Preview/snippet/reload actions | No Impala call, job creation, email, or SQL-file mutation |
| A01 | TUI/CLI/notebook equivalent inputs | Same plan/SQL/diagnostics, aside from presentation and job IDs |
| A02 | --yes and --acknowledge-advisor with M errors | Errors remain blocking |
| A03 | Old CLI/notebook/manifests | Existing behavior and public argument compatibility retained |

Use pure unit tests for scanner/calendar/renderer; property-style loops across
years and months may use stdlib without adding Hypothesis. Use mock runtime tests
for lifecycle; use fixtures/engine-independent evaluation to demonstrate semantic
counterexamples. Do not claim mock string matching proves actual Impala semantics.

## 15. Test/file ownership

New focused tests: `tests/test_monthly_domain.py` (calendar/scanner/rendering),
`tests/test_monthly_plan.py` (artifacts/consistency),
`tests/test_monthly_ui.py` (Pilot interactions), and
`tests/test_monthly_adversarial.py` (complex fixtures and documented counterexamples).
Use a dedicated `tests/fixtures/monthly/` directory for small/large synthetic SQL;
no user/production query data.

Extend existing `test_monthly_query_processor.py`, `test_runner_integration.py`,
`test_job_cli.py`, `test_notebook_api.py`, `test_prefill_seam.py`, `test_pure_logic.py`,
`test_mock_contract.py`, Advisor adapter/corpus tests, and relevant UI regression
tests. Reuse `mock_env`/`mock_env_with_config`; avoid starting real infrastructure
from unit tests. Do not weaken existing assertions to make new tests pass.

## 16. Required documentation and observability

Update README CLI examples; notebook API docs; in-product query-help/preview text;
ADR-0005 narrowly; Advisor spec for v2 parse scope; manifest/sidecar contract docs.
Add a short author example showing a slice-filtered purchases CTE and range-filtered
refunds CTE. Explain that the refund horizon is a business choice, not a recommended
universal join strategy.

Error logs may contain stable codes, versions, slice counts, and job IDs. Do not
add SQL bodies, snippets, source paths, or parameter dumps to telemetry. SQL remains
in private job artifacts and existing explicit previews/log surfaces as applicable.
Do not add a new monitoring service or emit per-slice completion events without
execution evidence.

## 17. Ordered implementation work packages

Each package must include a focused test result and a reviewer-readable summary.
Keep intermediate public behavior coherent: stage hidden/domain APIs first, then
enable user entry points only when their execution path is ready. Do not ship a
visible v2 form backed by the legacy renderer. Use ordinary dependency PRs or one
branch with coherent commits; no need to invent a permanent feature-flag framework.

| Package | Dependencies | Deliverable / files | Completion evidence |
|---|---|---|---|
| P0: contract/policy | None | Confirm baseline; narrow ADR-0005 and Advisor amendments; record engine/date gate and version-skew plan | Explicit architecture and compatibility review; no runtime change |
| P1: domain | P0 contract | monthly.py models/scanner/calendar/rendering; synthetic fixtures | C/L/R tests; repeated/quoted/large-file/source-map cases |
| P2: compilation | P1 | Full script builder and range-once preparation; hashes/JSON schema; output metadata | Golden structural script assertions; wrapper/UNION ALL/HDFS/terminator tests |
| P3: persistence | P2 | LaunchPlan, manifest/jobs publication, artifact validation; clone metadata | P01–P09 applicable; failure injection; no capacity leaks |
| P4: runner bridge | P3 and policy amendment | runner env, stdlib sidecar reader, narrow monthly processor branch | One-session tests; all mock scenarios; legacy pre/post equivalence |
| P5: shared launch/API | P4 | job_ops format/mode rules, CLI flags and summaries, notebook forwarding | A tests and legacy regression; actual v2 mock launch |
| P6: authoring UX | P5 | new_job month/mode controls, helper, full-file async discovery, generation lifecycle | Keyboard/Pilot validation; no silent mode change or SQL mutation |
| P7: review UX | P6 | monthly_preview views, reference navigation, slice/boundary/script views; shared confirmation | U/P tests; mock walkthrough at both terminal sizes |
| P8: diagnostics | P7 | mapped W001, Advisor v2 integration, legacy guidance, history/clone polish | Parse-unavailable and mapped-span tests; acknowledgement parity |
| P9: release validation | P8 | docs, complete test matrix, installed-layout checks, release evidence checklist | Full required checks; reviewer signoff and explicit Edge gaps |

P0–P4 can implement internal non-exposed APIs without changing legacy selection.
P5 exposes v2 CLI only after the bridge is complete. P6–P8 complete the feature's
TUI affordances; do not call the user-facing work done after CLI support alone.

For every handoff, include this exact information (plain prose/list is sufficient):

```text
Package completed:
Base/branch/commit:
Production files changed:
Requirements and test IDs satisfied:
Exact commands and results:
Known failures (new vs baseline):
Decisions deviating from spec (none unless explicitly resolved):
Next package and prerequisites:
Remaining production/release evidence:
```

## 18. Verification and release gates

Follow current repository checks if they change. At this baseline:

```sh
.venv/bin/python -m compileall dispatch scr
.venv/bin/ruff check dispatch tests
.venv/bin/ruff check scr
.venv/bin/ruff format --check dispatch tests
.venv/bin/mypy dispatch/sql.py dispatch/jobs.py dispatch/manifest.py
.venv/bin/python -m dispatch --help
.venv/bin/python -m pytest
```

Type-check the new domain module explicitly as well. Run focused tests per package,
then full tests at integration; repeat broad tests only for new changes or failures.
On Windows the committed PowerShell 5.1 local-check gate remains authoritative.
Consult AGENTS.md for known baseline failures; record rather than conceal them.

For scr changes enumerate every current JSON scenario under mocks/scenarios, not
just the list remembered in a skill. Run legacy pre/post versions with the same
scenario and compare logs, exit classification, email subjects/body, queue order,
retry behavior, and SQL shape; normalize only inherently variable timestamps/temp
roots and document normalization. V2 runs cover the same failure families and
assert the new script is executed once per attempt. Two-reviewer rule still applies.

Manually exercise mocked TUI at 80x24 and 120x40 from a directory containing SQL.
Exercise keyboard-only navigation, NO_COLOR, resize, invalid tokens, stale source,
copy failure, and one successful launch. Store evidence as PR attachments or task
artifacts, not committed screenshots/log dumps.

Installed-layout test: installed Dispatch + staged scr bundle, with no repository
on PYTHONPATH, can run v2 through mocks; missing old/new bundle combinations fail
closed. A direct stdlib-only orchestrator invocation must not import Dispatch,
Textual, or SQLGlot.

Release Operator evidence, when separately authorized, must verify the exact
CAST('YYYY-MM-DD' AS DATE) expression, DATE/TIMESTAMP boundaries, relevant partition
types, CTE CTAS, and UNION ALL behavior on supported Impala versions. Unsupported
DATE behavior blocks v2 release; it does not justify silently changing types.
Do not seek production credentials or deploy to satisfy local implementation tests.

## 19. Stop conditions and escalation

Stop the affected package, preserve completed work, and report a concrete conflict if:

- Current repository code materially contradicts source/destination or execution
  assumptions and implementing this design would require unrelated changes.
- The scanner cannot reliably distinguish a supported Impala lexical construct.
  Supply the smallest reproducer; do not fall back to global replacement.
- A shared source map cannot provide a truthful location; disable only that
  structural finding and report analysis unavailable, not parameter navigation.
- A proposed solution needs a new dependency, public orchestrator CLI change,
  retry change, email change, destination semantics change, or production operation.
- Existing manifest compatibility or snapshot identity cannot be preserved.
- Engine evidence contradicts the DATE contract or same-session behavior.

Continue independent local work where possible. A policy/engine release prerequisite
is not a reason to leave the complete local implementation and mock evidence unwritten.
No agent should guess a business rule, automatically relocate filters, or claim
semantic safety to finish a package.

## 20. Definition of done

All included UX and API paths work; required matrix passes or independently
documented baseline failures are acknowledged; legacy behavior is preserved;
the renderer is shared across preview and launch; artifact identity is tested;
SQL remains author-controlled; large-query navigation is bounded and complete;
scr remains stdlib-only; no new per-slice scheduling/retry semantics slipped in;
documentation matches implementation; and reviewers can reproduce local evidence.

Release readiness additionally requires normal repository reviewer/CI gates and
the separately authorized engine/installed-version evidence. A completed local
implementation and a production release are distinct outcomes.

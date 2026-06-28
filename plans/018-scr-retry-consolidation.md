# Plan 018: Finish `scr/` retry-loop consolidation per ADR-0005 (or record it as intentional)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- scr/_common.py scr/Query_Impala_Parametrized.py scr/download_to_csv.py scr/monthly_query_processor.py docs/adr/0005-scr-modification-policy.md`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P3
- **Effort**: M
- **Risk**: MED
- **Depends on**: plans/017-classifier-tests.md (the `_common` tests pin the
  classification behavior before this plan touches the retry wrappers)
- **Category**: tech-debt
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

ADR-0005 states the `scr/` consolidation target: a single `cycle_through_pools`
helper replacing three retry-loop implementations. The work is **half-done**:
`classificar_erro_impala` and `send_email` were reconciled into
`scr/_common.py`, but `Query_Impala_Parametrized.py:65-93` (`retry_loop`) and
`download_to_csv.py:50-67` (`retry_loop`) remain as per-script wrappers, each
re-declaring its own inline `operation`/`on_cycle_failure` closures and its
own queue list — and the two wrappers have **divergent queue sets**
(`Query_Impala_Parametrized.py:41` uses `["adhoc_fast","acs_small",
"adhoc_small","acs_large","adhoc"]` per ADR-0005; `download_to_csv.py:117`
uses `["adhoc_fast","adhoc_small","adhoc"]`). `monthly_query_processor.py`
already calls `cycle_through_pools` directly (the de-facto winner pattern).

This plan either finishes the consolidation or records the retained wrappers
as intentional, so ADR-0005's stated end state matches reality.

## Current state

`scr/_common.py:79-100` — `cycle_through_pools` (the shared helper):

```
79: def cycle_through_pools(pools, operation, on_cycle_failure, retry_interval=30, max_cycles=None) -> bool:
86:     retry_cnt = 1
87:     while True:
88:         for pool in pools:
90:             if operation(pool): return True
96:         on_cycle_failure(retry_cnt)
98:         time.sleep(retry_interval)
```

`scr/Query_Impala_Parametrized.py:65-93` — `retry_loop` wrapper (still
present, with its own closures and queue list at `:41`).

`scr/download_to_csv.py:50-67` — `retry_loop` wrapper (still present; queue
list at `:117` differs from `Query_Impala_Parametrized.py:41`).

`scr/monthly_query_processor.py:60` — calls `cycle_through_pools` directly
(the winner pattern).

**ADR-0005 process requirements** (from `docs/adr/0005-scr-modification-policy.md:43-53`):
1. PR description carries an explicit `[scr/]` tag + a paragraph: what
   changed, why it's safe, regression risk.
2. The change must run green against every scenario in `mocks/scenarios/`.
3. Two reviewers, one of whom has run the previous version in production.
4. Behavioural-equivalence proof: side-by-side log captures pre/post on the
   same mock scenarios.

This plan is the code change; the PR process requirements are operator
obligations noted in the STOP conditions and maintenance notes.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall scr`       | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |
| Mock scenarios | `source mocks/dev-env.sh && for s in happy_path all_queues_full memory_exceeded syntax_error auth_error slow; do DISPATCH_MOCK_SCENARIO=$s python -m pytest tests/test_mock_contract.py tests/test_runner_integration.py -q; done` | all pass per scenario |

## Scope

**In scope**:
- `scr/Query_Impala_Parametrized.py` — replace `retry_loop` with a direct
  `cycle_through_pools` call (or record why it stays).
- `scr/download_to_csv.py` — same.
- `docs/adr/0005-scr-modification-policy.md` — update the "single helper"
  wording if the wrappers are retained intentionally, OR add an amendment
  noting the consolidation is complete.

**Out of scope**:
- `scr/_common.py` `cycle_through_pools` — already the winner; no change.
- `scr/monthly_query_processor.py` — already calls `cycle_through_pools`
  directly; no change.
- The queue-list divergence (`Query_Impala_Parametrized.py:41` vs
  `download_to_csv.py:117`) — ADR-0005 locks the queue lists; do NOT unify
  them unless the ADR is amended. The two scripts legitimately use different
  pool sets; only the *wrapper structure* is consolidated.

## Git workflow

- Branch: `advisor/018-scr-retry-consolidation`
- Commit per step; message style: `[scr/] refactor: collapse retry_loop wrappers into cycle_through_pools` (the `[scr/]` tag is required by ADR-0005)
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Verify the queue-list divergence is intentional

Read `scr/Query_Impala_Parametrized.py:41` and `scr/download_to_csv.py:117`.
ADR-0005 (`:39-40`) locks `["adhoc_fast", "acs_small", "adhoc_small",
"acs_large", "adhoc"]` for the table-create path. Confirm whether
`download_to_csv.py`'s shorter list `["adhoc_fast","adhoc_small","adhoc"]`
is intentional (the CSV export may be scoped to faster pools only) or drift.
If it's drift, STOP and report — unifying queue lists is an ADR-level
decision, not this plan's scope. If it's intentional, leave both lists as-is
and only consolidate the wrapper structure.

### Step 2: Collapse `Query_Impala_Parametrized.py:retry_loop` into a direct `cycle_through_pools` call

Replace the `retry_loop` function (`:65-93`) and its call site (`:63`) with a
direct `cycle_through_pools` call that supplies the same `operation` and
`on_cycle_failure` closures inline, mirroring how
`monthly_query_processor.py:60` does it. Preserve the exact queue list, email
subjects, and message bodies — ADR-0005 locks them.

Before:
```python
def retry_loop(sql_query, filas, to_email, subject, tablecreated, user):
    messageBody = ...
    send_email(messageBody, subject_start, to_email)
    def operation(fila): ...
    def on_cycle_failure(retry_cnt): ...
    return cycle_through_pools(filas, operation, on_cycle_failure)

# call site:
retry_loop(sql_query, filas, to_email, subject, tablecreated, args.user)
```

After (inline at the call site, matching `monthly_query_processor.py`):
```python
messageBody = ...
send_email(messageBody, f"{subject} - PROCESSO INICIADO", to_email)
def operation(fila):
    sql_pool = f"set request_pool={fila};"
    return run_on_impala(sql_pool + " " + sql_query, subject, to_email, tablecreated, args.user, fila)
def on_cycle_failure(retry_cnt):
    ...  # identical body
return cycle_through_pools(filas, operation, on_cycle_failure)
```

Keep `filas` (`:41`) exactly as-is. Keep every email subject/body string
byte-identical (ADR-0005 locks email format).

### Step 3: Collapse `download_to_csv.py:retry_loop` the same way

Apply the same transformation to `download_to_csv.py:50-67`, preserving its
queue list (`:117`) and email strings exactly.

### Step 4: Run the mock-scenario equivalence proof

Run every scenario and capture the orchestrator output before and after the
change. ADR-0005 requires side-by-side log captures. Since this plan lands
both changes together, the "before" is `git stash`/the pre-commit tree; the
"after" is the working tree. Run:

```bash
source mocks/dev-env.sh
for s in happy_path all_queues_full memory_exceeded syntax_error auth_error slow; do
  DISPATCH_MOCK_SCENARIO=$s python -m pytest tests/test_mock_contract.py -q
done
python -m pytest tests/test_runner_integration.py -q
```

All must pass. The `test_mock_contract.py` tests assert the orchestrator+
mock-shell contract; `test_runner_integration.py` asserts the runner
lifecycle. If any scenario fails, the refactor changed behavior — STOP.

### Step 5: Update ADR-0005

In `docs/adr/0005-scr-modification-policy.md`, update the "What is allowed"
section's bullet about the retry-loop consolidation to reflect that it is
complete (or, if Step 1 found the wrappers are intentional, record why they
are retained). Add a dated amendment line.

**Verify**: `python -m compileall scr` → exit 0;
`python -m pytest tests -q` → all pass.

## Test plan

- No new tests — Plan 017 pins the classifier; the existing
  `tests/test_mock_contract.py` and `tests/test_runner_integration.py`
  assert behavioral equivalence. The verification is the scenario sweep in
  Step 4.
- Verification: all six mock scenarios pass `test_mock_contract.py`; the
  runner integration suite passes.

## Done criteria

- [ ] `python -m compileall scr` exits 0
- [ ] All six mock scenarios pass `tests/test_mock_contract.py` and
      `tests/test_runner_integration.py`
- [ ] `python -m pytest tests -q` exits 0
- [ ] `grep -n "def retry_loop" scr/Query_Impala_Parametrized.py` returns no
      matches (wrapper removed)
- [ ] `grep -n "def retry_loop" scr/download_to_csv.py` returns no matches
- [ ] `grep -n "cycle_through_pools" scr/Query_Impala_Parametrized.py` and
      `scr/download_to_csv.py` return matches (direct calls)
- [ ] `docs/adr/0005-scr-modification-policy.md` updated to reflect the
      consolidation status
- [ ] Queue lists in both scripts are byte-identical to pre-change
      (`git diff scr/Query_Impala_Parametrized.py` shows the `filas` line
      unchanged; same for `download_to_csv.py`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- Step 1 finds the `download_to_csv.py` queue-list divergence is **drift**,
  not intentional — STOP; unifying queue lists is an ADR-level decision
  outside this plan. Report and leave both wrappers in place.
- Any mock scenario fails in Step 4 — the refactor changed behavior; STOP and
  revert, then report which scenario and the diff in the log output.
- A reviewer with production experience (ADR-0005 requirement) is not
  available — STOP and report; the change cannot merge without the required
  review. The plan can still be prepared, but the PR cannot be opened.
- The inline `operation`/`on_cycle_failure` closures in `Query_Impala_Parametrized.py`
  or `download_to_csv.py` reference locals that would break under the inline
  transformation (e.g. a closure over a mutable that the wrapper previously
  isolated) — STOP and report; the transformation may need a small helper
  extraction instead of full inlining.

## Maintenance notes

- **ADR-0005 process**: the PR merging this change MUST carry the `[scr/]`
  tag, the safety paragraph, and the side-by-side log captures. The operator
  is responsible for assembling these; the plan's Step 4 produces the
  "after" captures, and the pre-commit tree produces the "before".
- The queue lists are now the only remaining per-script difference
  (`Query_Impala_Parametrized.py` uses 5 pools, `download_to_csv.py` uses 3).
  If a future ADR amendment unifies them, do it in a separate ADR-level
  change, not a refactor PR.
- Reviewer (production-experienced): confirm the email subjects and message
  bodies are byte-identical pre/post; any change is an ADR-0005 violation.
- If Step 1 concluded the wrappers are intentional, this plan becomes a
  docs-only change (update ADR-0005 to say the wrappers are retained) —
  record that decision and skip Steps 2-3.

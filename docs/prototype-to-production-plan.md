# Dispatch — Prototype to Production Plan

**Date:** 2026-05-16
**Baseline:** v1.0.0 (commit on `main`)
**Inputs:** `docs/plan.md`, ADRs 0001–0005, `CONTEXT.md`, screenshot review (`docs/ui-ux-screenshot-review-2026-05-16.md`), full codebase audit, test suite results (76/76 pass), lint results (flake8 clean, pylint 9.98/10).

---

## 1. Current State Assessment

### What exists and works

| Area | Status | Evidence |
|------|--------|----------|
| **Domain model** | Solid | `CONTEXT.md` is well-defined; Source/Destination/Job taxonomy is clean |
| **Manifest schema** | Production-ready | TypedDict with validation, schema versioning, all legal cells enforced |
| **Runner lifecycle** | Production-ready | Signal handling (SIGTERM/SIGHUP/SIGINT), graceful cancel with escalation, error files, state guards |
| **Mock layer** | Production-ready | 6 scenarios, fake impala-shell/klist/kinit, mock SMTP, full isolation per test |
| **Test suite** | Good foundation | 76 tests: unit (pure logic), integration (runner lifecycle), UI snapshots; all pass |
| **Lint quality** | Excellent | flake8 clean, pylint 9.98/10 |
| **Installer** | Functional | Idempotent, lock-protected, venv-based, handles offline vendor wheels |
| **TUI screens** | Functional prototype | All 7 screens render and navigate; forms submit; log tailing works |

### What is prototype-quality

| Area | Gap | Risk |
|------|-----|------|
| **Safety** | Zero confirmation dialogs for destructive actions (DROP TABLE, Cancel Job) | Data loss, wasted compute |
| **Form controls** | Source/Destination are free-text inputs | User errors, support burden |
| **Error handling** | Several exception paths surface raw Python tracebacks or silently fail | User confusion, lost work |
| **Navigation** | Inconsistent back-key bindings, phantom `? Help` hint, `v`/`a` overload | Learnability |
| **Pagination** | History page controls are static text — no actual navigation | Unusable for >17 jobs |
| **Selection UX** | Manual job-ID typing required everywhere; no row-to-action wiring | Friction, errors |
| **Feedback** | Single inline `#warning-text` for all messages; no toast/notification system | Missed errors, low confidence |
| **Edge-node validation** | Never tested on the actual production SSH chain + terminal | Unknown rendering issues |
| **Test coverage** | No screen-level interaction tests beyond dashboard; no error-path UI tests | Regression risk |
| **Observability** | No `dispatch.log` crash log; no startup error degradation (plan §8 specifies this) | Silent failures |

### Gap against `docs/plan.md` specification

The implementation plan (§8–§12) specifies several features and behaviors that are not yet implemented:

| Plan reference | Specified behavior | Current state |
|----------------|-------------------|---------------|
| §8 Startup seq. step 2 | Show "run install.sh" if config.json missing | App crashes with unhandled exception |
| §8 Startup seq. step 4 | Dim launch button if Kerberos missing | Button is always enabled; validation catches late |
| §8 Crash-resistance | Any step 2–5 failure logs to `~/.dispatch/dispatch.log` | No crash log exists |
| §8 Dashboard wireframe | Elapsed time column in active jobs table | Not implemented |
| §8 New Job wireframe | Source as radio buttons, not text input | Free-text input |
| §8 New Job wireframe | Destination as radio buttons, not text input | Free-text input |
| §8 New Job wireframe | Date range hidden unless SqlTemplate | Always visible |
| §8 New Job wireframe | `[Shift-N] New blank` for SQL file creation | Not implemented |
| §8 Browser wireframe | `[E]xport to CSV` action on selected table | Not implemented |
| §12 PR #11 | Hard-delete legacy GUI files | `run_query.ps1` / `run_query_engine.bat` already deleted, but README still references legacy |

---

## 2. Production Quality Criteria

A production-ready Dispatch must satisfy:

1. **Safety:** No destructive action executes without confirmation.
2. **Correctness:** Every plan-specified behavior is implemented or explicitly deferred with a documented reason.
3. **Resilience:** The TUI degrades gracefully under missing config, expired Kerberos, unreadable manifests, and terminal resize.
4. **Testability:** Every screen has automated interaction tests; error paths are covered.
5. **Operability:** Crash logging, startup diagnostics, and version-mismatch warnings work end-to-end.
6. **Edge-node validation:** Smoke-tested over the real SSH chain with the actual terminal emulator.

---

## 3. Work Breakdown

Work is organized into five tracks that can proceed mostly in parallel. Each track has a clear definition of done.

### Track A: Safety & Confirmation Modals

**Goal:** No destructive action fires without explicit confirmation.

| # | Task | Scope | Definition of done |
|---|------|-------|--------------------|
| A1 | Create a reusable `ConfirmScreen` modal | New file: `dispatch/screens/confirm.py` | A `Screen` subclass that accepts a title, body, and danger-level; returns `True`/`False` via callback. Renders with a clear "confirm or cancel" layout, responds to `Y`/`N`/`Enter`/`Escape`. |
| A2 | Wire `ConfirmScreen` to DROP TABLE | `screens/browser.py` | Pressing `D` opens ConfirmScreen. Only on confirmation does `impala.drop_table()` execute. Test: automated pilot test proves DROP is gated. |
| A3 | Wire `ConfirmScreen` to Cancel Job | `screens/job_detail.py` | Pressing `C` opens ConfirmScreen showing job ID and PID. Only on confirmation does `process.cancel_process_group()` fire. Test: automated pilot test. |
| A4 | Wire `ConfirmScreen` to Launch Job | `screens/new_job.py` | Pressing `L` opens a lightweight confirmation showing source, destination, and target table. Immediate launch on confirm. Test: pilot test. |

### Track B: Form Controls & Validation

**Goal:** Constrained inputs replace free-text fields; form behavior matches plan wireframes.

| # | Task | Scope | Definition of done |
|---|------|-------|--------------------|
| B1 | Replace Source input with `Select` widget | `screens/new_job.py` | Dropdown with `SqlFile`, `SqlTemplate`, `ExistingTable`. Changing selection dynamically disables illegal Destination options. |
| B2 | Replace Destination input with `Select` widget | `screens/new_job.py` | Dropdown with `Table`, `Csv`, `Table+Csv`. Illegal cells greyed out based on selected Source. |
| B3 | Conditional date range visibility | `screens/new_job.py` | Start Date / End Date fields are hidden unless Source is `SqlTemplate`. Shown/hidden reactively on Source change. |
| B4 | Dynamic date defaults | `screens/new_job.py` | Default start = first day of current month; default end = last day of current month. Overridable. |
| B5 | Dim Launch button when Kerberos missing | `screens/new_job.py` | Button `disabled=True` when `self.kerberos_ttl` is `None` or < 300. Re-enabled on successful kinit. |
| B6 | Inline field validation feedback | `screens/new_job.py` | As fields change, show `✓`/`✗` indicators. Email gets format validation. SQL file gets existence check. |
| B7 | Persist last-used form values | `config.py`, `screens/new_job.py` | On successful launch, write `schema`, `email`, `destination_type` to config. Pre-fill on next New Job. |
| B8 | Fix Preview Launch button semantics | `screens/preview.py` | Either wire to actual job submission (calling back into new_job logic), or rename to "Accept & Return" with clear copy that the user must press Launch on the New Job screen. |

### Track C: Navigation, Selection & Feedback

**Goal:** Consistent navigation, row-based selection, and a proper notification system.

| # | Task | Scope | Definition of done |
|---|------|-------|--------------------|
| C1 | Normalize back-key bindings | All non-root screens | Every non-root screen binds both `Escape` and `b` to `app.pop_screen`. `NewJobScreen` currently missing `b`. |
| C2 | Row selection → auto-fill Job ID (Dashboard) | `screens/dashboard.py` | When a table row is clicked or highlighted via keyboard, the `#job-id` Input is auto-populated with the full job ID. |
| C3 | Row selection → View Logs (History) | `screens/history.py` | `Enter` on a highlighted row navigates to `JobDetailScreen` for that row's job ID. Separate `#job-id` Input retained as fallback. |
| C4 | Implement pagination keybindings (History) | `screens/history.py` | Add `[` (prev page) and `]` (next page) bindings. Wire to `self._page ± 1` + `refresh_history()`. Show page controls as real `Button` widgets. |
| C5 | Implement `?` Help screen or remove hint | Global | Either create a `HelpScreen` listing all keybindings per screen, or remove `? Help` from sidebar footer. Recommended: implement the help screen. |
| C6 | Add toast notifications via `self.notify()` | Global | Replace inline `#warning-text` updates with Textual's built-in notification system for: launch success, validation errors, Kerberos warnings, DROP results. Keep inline text for persistent status only. |
| C7 | Resolve `v`/`a` binding ambiguity | `screens/dashboard.py` | Either (a) make `a` do something distinct (attach to live-streaming log, auto-scrolling) vs `v` (view static snapshot), or (b) remove the `a` binding and keep only `v`. |
| C8 | Rich empty states | `screens/dashboard.py`, `screens/history.py` | Replace `(none)` table rows with styled empty-state blocks: "No active jobs — press N to create one" / "No history — jobs older than 7 days appear here". |
| C9 | Show elapsed time for running jobs | `screens/dashboard.py`, `screens/job_detail.py` | Add an "Elapsed" column to the active jobs table. Show "Running for 2m 34s" in Job Detail summary, updated every second. |

### Track D: Resilience, Observability & Error Handling

**Goal:** The TUI never crashes with a raw traceback; failures are logged and surfaced gracefully.

| # | Task | Scope | Definition of done |
|---|------|-------|--------------------|
| D1 | Crash log at `~/.dispatch/dispatch.log` | `dispatch/app.py` | Unhandled exceptions during startup and runtime are caught and written to a rotating log file. The TUI shows a non-blocking "check dispatch.log" notification. |
| D2 | Graceful startup when config.json missing | `dispatch/app.py` | Instead of crashing, show a single-screen message: "Dispatch is not installed for this user. Run `install.sh` to set up." with a Quit binding. |
| D3 | Safe SQL file reads in Preview/Launch | `screens/new_job.py`, `screens/preview.py` | Wrap all `_read_sql()` calls in try/except. Surface actionable error: "Cannot read {path}: {error}. Check the file exists and is readable." |
| D4 | Minimum terminal size detection | `dispatch/app.py` | On mount and resize, check if terminal is smaller than 80×24. If so, show a warning banner: "Terminal too small (current: {w}×{h}, minimum: 80×24)". |
| D5 | Graceful Kerberos unavailability | `screens/dashboard.py`, `screens/new_job.py` | If `klist` is not on `PATH`, show "Kerberos: unavailable" instead of crashing. Log the error. |
| D6 | Handle corrupt/unreadable manifests | `dispatch/jobs.py` | `list_manifests` already catches exceptions per-manifest. Ensure the error is logged (not silently swallowed) and a notification is shown if any manifests fail to load. |
| D7 | Timeout on slow Impala metadata queries | `screens/browser.py` | Show a loading indicator while `SHOW TABLES` / `DESCRIBE` are in progress. Surface timeout errors clearly. |

### Track E: Test Coverage & Edge-Node Validation

**Goal:** Every screen has automated interaction tests; the app is validated on the real deployment target.

| # | Task | Scope | Definition of done |
|---|------|-------|--------------------|
| E1 | Pilot tests for every screen | `tests/test_ui_screens.py` (new) | Automated Textual pilot tests that navigate to each screen, interact with key widgets, and assert expected content. Cover: Dashboard, New Job, Preview, Job Detail, History, Browser. |
| E2 | Error-path UI tests | `tests/test_ui_error_paths.py` (new) | Tests for: missing SQL file in Preview, missing config on startup, expired Kerberos, illegal Source×Destination cell, concurrency cap hit. |
| E3 | Confirmation modal tests | `tests/test_confirm_screen.py` (new) | Tests proving that DROP, Cancel, and Launch are gated behind confirmation. |
| E4 | Form constraint tests | `tests/test_new_job_form.py` (new) | Tests that Select widgets constrain choices, illegal cells are disabled, date fields hide/show reactively. |
| E5 | Edge-node smoke test checklist | `docs/edge-node-smoke-test.md` (new) | Manual checklist for validating on the real Edge Node: terminal rendering, Kerberos auth, impala-shell connectivity, SSH disconnect survival, install.sh on real `/ads_storage/`. |
| E6 | Snapshot regression tests for all screens | `tests/test_ui_snapshots.py` (extend) | Extend existing snapshot tests to cover New Job, Preview, Job Detail, History, and Browser screens. Each asserts key content strings are present. |

---

## 4. Dependency Graph

```
Track A (Safety)         Track B (Forms)         Track C (Nav/UX)         Track D (Resiltic)      Track E (Tests)
──────────────           ──────────────          ──────────────           ──────────────           ──────────────
A1: ConfirmScreen ──┐    B1: Source Select       C1: Normalize back      D1: Crash log            E1: Screen pilots
A2: DROP confirm  ←─┤    B2: Dest Select         C2: Row→fill (Dash)     D2: Missing config       E2: Error paths
A3: Cancel confirm←─┤    B3: Cond. dates         C3: Row→logs (Hist)     D3: Safe SQL reads       E3: Confirm tests ← A1
A4: Launch confirm←─┘    B4: Dynamic dates       C4: Pagination keys     D4: Min terminal size    E4: Form tests ← B1,B2
                         B5: Dim Launch btn       C5: Help screen         D5: Kerberos graceful    E5: Edge-node checklist
                         B6: Inline validation    C6: Toast notifs        D6: Corrupt manifests    E6: Snapshot regression
                         B7: Persist values       C7: v/a ambiguity       D7: Loading indicators
                         B8: Fix Preview Launch   C8: Empty states
                                                  C9: Elapsed time
```

**Hard dependencies:**
- A2, A3, A4 all depend on A1 (ConfirmScreen).
- E3 depends on A1 (needs ConfirmScreen to test).
- E4 depends on B1, B2 (needs Select widgets to test).

**Everything else is parallelizable.** Tracks A–D can be worked simultaneously by different contributors or sequential agents. Track E should trail slightly behind the other tracks so tests cover the new code.

---

## 5. Implementation Sequence

For a single contributor working sequentially, the recommended order optimizes for "most dangerous gaps closed first":

### Phase 1: Safety (blocks production use)

1. **A1** → **A2** → **A3** → **A4** — Confirmation modals. ~4 tasks, small scope each.
2. **D3** — Safe SQL file reads (prevents crashes on common user error).
3. **B8** — Fix Preview Launch button (prevents user confusion about job state).

**Exit criterion:** No destructive or irreversible action fires without confirmation. No crash on missing SQL file.

### Phase 2: Correctness (closes plan gaps)

4. **B1** → **B2** → **B3** — Constrained form controls. Medium scope.
5. **B5** — Dim Launch when Kerberos missing.
6. **C1** — Normalize back keys (tiny).
7. **C2** → **C3** — Row selection wiring.
8. **C4** — Pagination.

**Exit criterion:** New Job form matches plan wireframe. All screens have consistent navigation. History is usable with >17 jobs.

### Phase 3: Resilience & Observability

9. **D1** → **D2** — Crash log + missing-config graceful degradation.
10. **D4** → **D5** — Terminal size + Kerberos graceful.
11. **D6** → **D7** — Corrupt manifests + loading indicators.

**Exit criterion:** The TUI never shows a raw Python traceback to the user.

### Phase 4: Polish & Feedback

12. **C5** — Help screen.
13. **C6** — Toast notifications.
14. **C7** → **C8** → **C9** — v/a ambiguity, empty states, elapsed time.
15. **B4** → **B6** → **B7** — Dynamic dates, inline validation, persist values.

**Exit criterion:** The TUI feels responsive, discoverable, and polished.

### Phase 5: Test Hardening & Validation

16. **E1** → **E2** → **E3** → **E4** — Automated screen tests.
17. **E6** — Snapshot regression tests for all screens.
18. **E5** — Edge-node smoke test (requires access to production environment).

**Exit criterion:** All automated tests pass. Edge-node smoke test checklist is green.

---

## 6. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Textual `Select` widget doesn't support dynamic option disabling | Medium | Delays Track B | Fall back to `RadioSet` or custom widget. Verify early with a spike. |
| Textual `self.notify()` positioning conflicts with footer on small terminals | Low | Minor | Test at 80×24 minimum size; fall back to inline text. |
| Edge-node terminal emulator doesn't render Textual correctly | Medium | Blocks release | ADR-0002 already identifies this risk. Test early in Phase 5. Have `urwid` fallback ready. |
| ConfirmScreen keyboard focus conflicts with parent screen bindings | Low | Delays Track A | Use `push_screen` with focus isolation (Textual's default for new screens). |
| Persisting form values in config.json causes schema drift | Low | Minor | Add a `"form_defaults"` nested key; validate on read; ignore corrupt values. |

---

## 7. Definition of Done — Production Release

All of the following must be true before the version is considered production-ready:

- [ ] All Phase 1–4 tasks are complete and merged.
- [ ] All automated tests pass (76 existing + new screen/form/confirm tests).
- [ ] flake8 clean, pylint ≥ 9.9/10.
- [ ] Edge-node smoke test checklist (`docs/edge-node-smoke-test.md`) is green.
- [ ] No `TODO`, `FIXME`, or `HACK` comments in `dispatch/` (or each has a linked issue).
- [ ] `docs/plan.md` wireframes match the actual UI (or deviations are documented as intentional).
- [ ] VERSION bumped to `1.1.0` (or `2.0.0` if the form control changes are considered breaking for muscle-memory users).
- [ ] `CONTEXT.md` and `README.md` updated to reflect any new concepts or changed behavior.
- [ ] Prior UI/UX review findings (`docs/ui-ux-screenshot-review-2026-05-16.md`) are all addressed or explicitly deferred with linked issues.

---

## 8. What This Plan Does NOT Cover

These are explicitly out of scope, consistent with `docs/plan.md` §15:

- Auto-queueing a 3rd Job when 2 slots are full.
- Cross-user Job visibility.
- Cluster / queue health dashboard.
- Mid-Job Kerberos auto-renewal.
- Staging-cluster integration test environment.
- Resume-from-failure for partially-completed `Table + Csv` Jobs.
- `scr/` orchestrator refactoring (governed by ADR-0005, separate track).
- `vendor/` wheel management and offline install path (already functional).

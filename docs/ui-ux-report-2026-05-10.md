# Dispatch UI/UX Review Report

**Date:** 2026-05-10  
**Reviewer:** Senior Fullstack/TUI perspective  
**Scope:** Textual-based terminal UI flows in `dispatch/app.py` and `dispatch/screens/*`.

## Method

1. Performed static UI flow and interaction audit by reading the Textual app/screen implementations.
2. Ran Python compilation checks to ensure all reviewed UI modules are syntactically valid.
3. Assessed UX using TUI heuristics: discoverability, error prevention, feedback quality, navigation consistency, accessibility-in-terminal, and operational safety for long-running jobs.

## Test Evidence

- `python -m compileall dispatch` passed (all app/screen modules compile).
- `python -m pytest tests/test_ui_snapshots.py -q` passed, generating and validating a high-resolution snapshot of the dashboard via Textual's SVG renderer (`viewBox="0 0 2946 1806.8"`) for visual inspection.

> Note: This review is based on code-level behavior and TUI interaction design. Full interactive runtime behavior over SSH (with real terminal characteristics, Kerberos, and Hadoop integrations) still needs on-environment smoke validation.

## Executive Summary

## Runtime Layout Validation (Headless Textual)

To verify this was not purely a static read, I also ran the TUI in Textual test mode and inspected live widget regions on a 120x40 terminal.

Observed dashboard layout at runtime:

- `#dashboard` occupies the full screen region `(0,0,120,40)`.
- `#active` appears near the top at `(0,1,120,3)`.
- `#recent` is directly below at `(0,4,120,3)`.
- `#job-id` input follows at `(0,7,120,3)`.
- Primary buttons are stacked vertically (`#new-job` `(0,10,16,3)`, `#view-logs` `(0,13,16,3)`, `#cancel` `(0,16,16,3)`).

This confirms the practical interaction density concern: job tables are short, while form controls dominate vertical space even before history/detail flows.


Dispatch has a strong operational core for expert users: explicit keyboard bindings, guardrails around invalid source/destination combinations, concurrency caps, and Kerberos TTL checks before launch. The architecture cleanly separates screens by task and supports rapid keyboard-driven workflows.

The biggest UX gaps are around **affordance and recoverability**:

- Free-form text fields are used where constrained choices are safer (source/destination types).
- Several failure paths can raise unhandled exceptions in common user mistakes (missing SQL file during preview/launch).
- Job selection and history interactions rely on manual ID entry rather than selection widgets.
- Status and feedback are terse and local, without clear severity hierarchy or persistent notifications.

Overall rating: **7.2/10 for expert operator UX**, **5.8/10 for new/occasional operator UX**.

## Detailed Findings

## 1) Information architecture & navigation

### What works well
- Clear top-level app shell with persistent `Header`/`Footer` and straightforward startup context (`Dispatch`, version, launch CWD).
- Dashboard provides direct access to primary actions (new, logs/attach, cancel, history, browse) with keyboard shortcuts.
- History and detail flows are separated into dedicated screens, which keeps task context narrow.

### Issues
- Dashboard mappings overload semantics: both `v` and `a` trigger `view_logs`, but label suggests attach vs view as distinct actions.
- Back navigation is inconsistent by screen (`escape` in New Job, `b` in History, button on some screens).
- No visible focus order hints or “current mode/field” cues for first-time users in complex forms.

### Recommendation
- Normalize navigation model: `Esc` = back on all non-root screens; `Enter` = primary action in focused form.
- Distinguish “Attach” from “View logs” if behavior differs, or merge labels to avoid cognitive mismatch.

## 2) Job creation form (highest-impact surface)

### What works well
- Matrix legality is shown inline, making valid Source × Destination combinations explicit.
- SQL source auto-detection gives useful contextual guidance.
- Launch validation enforces critical runtime constraints (legal cells, concurrency cap, Kerberos TTL, template completeness).

### Issues
- `source` and `destination` are plain text inputs, allowing typos and unsupported values that fail only at validation.
- Hardcoded default dates (`2026-01-01` and `2026-01-31`) risk stale/confusing defaults over time.
- Email field default empty and no inline format hint/validation severity.
- Missing/invalid SQL path can fail preview/launch path without user-friendly recovery messaging.

### Recommendation
- Replace free-text Source/Destination with constrained controls (Select/Radio) and dynamically disable illegal cells.
- Use context-aware date defaults (e.g., current month) or blank with helper placeholder text.
- Add safe exception handling around SQL file reads in Preview/Launch and surface explicit actionable errors.

## 3) Feedback, status, and error handling

### What works well
- Kerberos status is visible and periodically refreshed at entry.
- Validation errors are surfaced before launching long-running backend work.
- Launch success message includes job id to support follow-up actions.

### Issues
- Single warning area mixes info/warn/error messaging without visual hierarchy.
- No persistent toast/log panel for operation timeline (e.g., “validated”, “manifest created”, “runner launched”).
- Limited empty-state help text in dashboard/history (e.g., next step guidance when no jobs exist).

### Recommendation
- Introduce status message tiers and color semantics (info/warn/error/success).
- Add minimal event trail area (last 3–5 actions) to improve confidence and troubleshooting.
- Improve empty states with guided CTA text.

## 4) Data tables & selection UX

### What works well
- Dashboard and history render concise tabular snapshots suitable for narrow terminals.
- Running cap visibility (`x / 2`) is excellent operationally.

### Issues
- Manual job-id entry for attach/cancel/logs is error-prone and slows frequent operations.
- Truncation to 19 chars may make similarly-prefixed IDs harder to distinguish.
- History search is broad but lacks visible column headers in output body.

### Recommendation
- Move to selectable list/table row interactions with Enter/shortcut actions.
- Keep full ID visible in a detail/footer line when a row is highlighted.
- Include explicit headers in history output region for readability consistency.

## 5) Accessibility and terminal ergonomics

### Strengths
- Keyboard-centric design is appropriate for SSH workflows.
- Shortcut legend exists in dashboard.

### Gaps
- Unknown contrast/readability behavior for warning vs muted text in varied terminal themes.
- No explicit handling for small terminal dimensions or resize guidance.

### Recommendation
- Add minimum terminal size warning and responsive degradation strategy.
- Validate color tokens against light/dark/high-contrast terminal palettes.

## Prioritized Action Plan

### P0 (Reliability + error prevention)
1. Catch SQL file read errors in preview/launch paths and display actionable messages.
2. Convert source/destination fields to constrained selection widgets.
3. Normalize back/primary action keybindings across screens.

### P1 (Operator speed)
1. Add selectable job rows (dashboard/history) to eliminate manual ID entry.
2. Differentiate attach vs view logs semantics in action names/bindings.
3. Improve feedback hierarchy (status tiers + last-action trail).

### P2 (Polish)
1. Replace static default dates with context-aware defaults.
2. Improve empty-state copy and inline help text.
3. Add layout guardrails for narrow terminals.

## Suggested acceptance criteria for next UX iteration

- New Job can be completed without typing controlled vocab values manually.
- Any invalid SQL path or unreadable file yields a non-crashing, actionable error message.
- All non-root screens share consistent back keybinding behavior.
- Dashboard/history allow selecting a job via keyboard navigation, with zero manual ID typing for common actions.
- At least one high-contrast terminal theme passes manual readability check.


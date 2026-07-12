# Advisor Review Fixes Design

Date: 2026-07-12

## Problem

PR #44 adds the Query Optimization Advisor, but review found correctness,
responsiveness, layout, and repository-standard defects. The most serious
defects can suppress an R07 launch gate, crash analysis on tokenizer errors,
and gate valid `JOIN ... USING` SQL as Cartesian.

This work ships as a fixes-only PR stacked on
`cursor/query-optimization-advisor-7cf0`.

## Goals

- Make analysis unavailable, rather than raising, for every SQLGlot parse or
  tokenization failure.
- Make R04, R05-R07, R09, R16, and R17 conform to the signed-off rule catalog.
- Keep live New Job validation responsive without repeatedly reading SQL on the
  Textual event loop.
- Keep every launch-gate action visible and keyboard reachable at 80x24.
- Restore all required formatting and import standards.

## Non-goals

- Redesigning the Advisor public API or rule catalog.
- Adding metadata, suppression, rewriting, or new Advisor rules.
- Changing launch, preview, manifest, or `scr/` semantics.
- Folding the fixes into PR #44's branch.

## Approaches considered

### A. Focused fixes at existing seams (chosen)

Keep `AnalysisResult`, `analyze()`, and the existing screens. Correct adapter
error handling and rule aggregation, make unavailable structural analysis
coexist with form Findings, cache the live SQL text loaded by a worker, and
give the launch gate dedicated bounded styling.

This keeps the stacked diff small while covering every review finding.

### B. Split structural and form analysis into new result types

Represent SQL availability and form checks as separate nested result objects.
This makes partial availability explicit but changes every consumer and test
for one display bug.

### C. Split engine and TUI fixes into separate PRs

This narrows each diff but leaves either PR incomplete against the requested
review and complicates ordering on top of an already stacked feature.

## Architecture

### Adapter and rule engine

`adapt()` catches both SQLGlot `ParseError` and `TokenError` and returns an
unavailable `AdapterResult`.

R05-R07 are evaluated once across the successful parse:

- count parsed join-side table occurrences by canonical table key
- count adapter-recorded hints by their recorded table key
- emit R05 when a known table has more joins than recorded hints
- evaluate every recorded hint independently for R06/R07

This uses the adapter's source-bound hint/table record and avoids assigning the
first same-name hint to every AST join. R08 remains query-block structural
analysis.

R09 treats a populated SQLGlot `using` argument as a join condition. R17 adds
only `DROP TABLE IF EXISTS` statements to the satisfied-table set. R04 compares
the upper date with the lower date advanced by 13 clamped calendar months, so
an additional day fires.

### Analysis availability and form Findings

`AnalysisResult.available` continues to mean structural SQL availability.
When unavailable analysis still carries R16, badge and Preview rendering show
those form Findings and append an explicit structural-analysis-unavailable
note. With no form Findings, the existing unavailable display remains.
Unavailable structural analysis never creates an error gate.

### Live New Job analysis

The screen keeps a path/text cache used only for live Advisor display. A
Textual worker reads a newly selected SQL path with `asyncio.to_thread`; stale
worker results are discarded when the current path changes. Unrelated form
edits reuse the cached text and only recompute in-memory analysis.

Preview and launch retain their existing fresh on-disk read, preserving the
contract that the original current file is the sole preview/launch input.

### Launch gate

`AdvisorLaunchGate` uses dedicated IDs and CSS rather than borrowing
`ConfirmScreen` selectors. The modal is centered and height-bounded. Only the
findings region scrolls; title, explicit proceed/cancel guidance, and buttons
remain visible and keyboard reachable at 80x24.

## Error handling

- SQLGlot tokenizer/parser failures produce unavailable analysis.
- Live cache read failures clear the cached SQL and update the summary without
  raising into Textual.
- A completed cache worker paints only when its path still matches the form.
- Launch and Preview continue to report their existing file-read failures.

## Testing

Tests use the existing public seams:

1. `adapt()` returns unavailable for unterminated SQL.
2. `analyze()` covers repeated same-table hints, a missing hint among repeated
   joins, `JOIN ... USING`, partial-month R04, plain-drop R17, and R16 during
   unavailable structural analysis.
3. New Job Pilot interaction proves unrelated input changes do not reread SQL.
4. Preview renders R16 plus the unavailable note.
5. Launch-gate Pilot interaction at 80x24 proves its title, guidance, and
   buttons remain in the viewport and findings scroll.
6. Existing confirm/cancel and original-SQL launch behavior remain green.

Each behavior change follows red-green TDD. Final validation runs focused
Advisor tests, Ruff lint/format, compile, mypy, the full pytest suite, and a
mocked TUI walkthrough at 80x24 and 120x40.

## Success criteria

- All ten review findings are fixed or covered by the dedicated gate/layout
  correction.
- New regression tests fail on PR #44's head and pass on the fix branch.
- Required CI-equivalent checks pass except any documented pre-existing
  onboarding mismatch.
- The stacked PR contains only the design, regression tests, and focused fixes.

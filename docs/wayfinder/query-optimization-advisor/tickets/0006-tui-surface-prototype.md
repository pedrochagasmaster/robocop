---
title: "Prototype: where the advisor lives in the TUI"
labels: [wayfinder:prototype]
status: closed
assignee: cursor-agent
blocked-by:
  [0004-advisory-vs-rewrite, 0005-enforcement-policy, 0009-scoring-model]
---

## Question

Where and how do findings (and the rating, if one exists) surface in the
Dispatch TUI? Build a cheap Textual prototype (mock findings, no real
analysis) for the sponsor to react to.

Candidate surfaces, from least to most invasive:

1. **Inline in the New Job screen's validation summary** — the screen already
   runs `_validation_issues()` live and shows "Ready to launch"; findings
   append there with severity coloring.
2. **In the Preview SQL screen** (`dispatch/screens/preview.py`) — findings
   as a panel beside the highlighted SQL, close to the evidence and manual
   remediation guidance. This remains read-only; it is not a diff or editor.
3. **A dedicated Analyze screen/action** — explicit `a`-keybinding step
   between compose and launch. Metadata-backed checks are out of v1 per the
   [metadata availability research](0003-metadata-availability-research.md),
   and v1's static analysis is fast enough to need no worker/spinner surface —
   evaluate this option as future-proofing for the deferred metadata effort,
   not as a v1 requirement.
4. **Launch-time gate only** — a modal listing findings when the user hits
   Launch, with proceed/cancel (mandatory anyway if any finding blocks, per
   the enforcement decision).

Constraints from the Dispatch TUI skill: keyboard-first, no event-loop
blocking (analysis in a worker), severity conveyed by label + color (never
color alone), readable at 80x24 over SSH, follow the `.action-bar` and
three-panel conventions.

Link the prototype branch/screenshots as assets. The answer records the
chosen surface(s) and interaction flow.

## Resolution

Prototyped and approved by the sponsor on 2026-07-11. The advisor surfaces
on **three composed surfaces** — candidates 1, 2, and 4 — with the dedicated
Analyze screen (candidate 3) left as future-proofing for the deferred
metadata effort:

1. **New Job validation summary**: the worst-severity badge with severity
   counts (`Advisor: error (1 error · 1 warning · 2 infos)`) sits beside
   "Ready to launch" in the action bar, updating live as the form changes.
2. **Preview SQL screen**: a findings panel below the highlighted SQL shows
   every finding in the locked two-part shape (diagnostic detection line,
   then the typed remediation), each with rule id + guideline reference;
   the badge repeats in the action bar.
3. **Launch gate**: a modal listing only error findings with explicit
   proceed/cancel, stating that the SQL launches exactly as written.

Interaction flow: static analysis runs automatically on form changes (fast
enough to need no worker/spinner), the badge updates live, `P` opens the
full findings in Preview, `L` gates only when error findings exist.
Severity is always conveyed label-first, never color alone; the layout
holds at 80x24.

Prototype:
[prototype_advisor_surface.py](../assets/prototype_advisor_surface.py)
(throwaway, runnable standalone). Screenshots:
[New Job badge, errors](../assets/prototype_newjob_badge_errors.svg) ·
[New Job badge, clean](../assets/prototype_newjob_badge_clean.svg) ·
[Preview findings panel](../assets/prototype_preview_findings.svg) ·
[launch gate](../assets/prototype_launch_gate.svg) ·
[Preview at 80x24](../assets/prototype_preview_80x24.svg).

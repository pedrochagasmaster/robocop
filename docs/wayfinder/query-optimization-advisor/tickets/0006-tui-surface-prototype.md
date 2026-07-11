---
title: "Prototype: where the advisor lives in the TUI"
labels: [wayfinder:prototype]
status: open
assignee: none
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

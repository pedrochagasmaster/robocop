---
title: "Decide: can findings block a launch, or only inform"
labels: [wayfinder:grilling]
status: closed
assignee: cursor-agent
blocked-by: [0001-rule-catalog]
---

## Question

Dispatch already **hard-refuses** launches for illegal Source/Destination
combinations, missing Kerberos tickets, and a third simultaneous Running Job
(product invariant in `.agents/skills/dispatch-textual-tui/SKILL.md`). Do any
advisor findings join that list, or is the advisor purely informative?

Grill through the severity tiers the rule catalog produces:

- Are there guidelines so unconditionally bad (e.g. unfiltered `SELECT *`
  from `core.cut_clear_dtl_enc`, a >13-month date range that Impala will
  reject anyway) that launch should be refused or require an explicit
  override keystroke?
- False-positive tolerance: a static analyzer will sometimes be wrong.
  Blocking on a wrong finding strands a legitimate Job on a production edge
  node — what override path exists (per-launch "launch anyway", per-rule
  suppression comment in the SQL, config opt-out)?
- Does the answer differ by Source? `SqlTemplate` and `ExistingTable` Jobs
  have different exposure (an `ExistingTable -> Csv` Job has no SELECT to
  analyze at all).

The answer is a policy table: severity tier -> launch behavior (block /
confirm / warn / info) plus the override mechanism.

## Resolution

Grilled with the sponsor on 2026-07-10. **Confirm is the enforcement
ceiling — no advisor finding ever hard-blocks a launch.** Dispatch's
existing hard-refusals are certainties; a static analyzer is not, and a
false positive must never strand a legitimate Job on a production edge
node.

| Severity tier | Launch behavior |
|---|---|
| error | Launch pauses on a modal listing the error findings; the user explicitly proceeds or cancels. The friction is the enforcement. |
| warning | Surfaced wherever the surface prototype puts findings; never gates the launch flow. |
| info | Displayed only. |
| analysis unavailable (parse failure, unsupported syntax) | Never gates; at most an info note. A broken analyzer must never break launching. |

Override machinery: the per-launch confirm is the **only** override in v1.
Per-rule suppression comments and config opt-outs are ruled **out of scope**
for this destination — suppression pressure is low (only errors gate, and
they are the high-confidence set), suppression syntax invites stale ignore
comments, and a wholesale opt-out defeats the effort. If real false-positive
data proves the friction unacceptable, suppression returns as its own
designed feature in a fresh effort.

Per Source: `ExistingTable` produces no findings, so nothing gates it;
`SqlFile` and `SqlTemplate` receive identical treatment.

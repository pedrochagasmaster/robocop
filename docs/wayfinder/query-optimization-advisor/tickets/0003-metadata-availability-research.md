---
title: "Research: what Impala metadata is available at pre-launch analysis time"
labels: [wayfinder:research]
status: closed
assignee: cursor-agent
blocked-by: []
---

## Question

Several manual guidelines need more than the SQL text: broadcast-vs-shuffle
depends on table size (`SHOW TABLE STATS`), partition-pruning verification
suggests `EXPLAIN`, stats freshness suggests `SHOW TABLE STATS`. Can the
advisor obtain any of this at the moment a user is composing a Job in the
New Job screen — and should it?

Investigate:

1. **Existing plumbing** — `dispatch/impala.py` already runs metadata queries
   (`SHOW TABLES`, `DESCRIBE`) through `impala-shell` with a 30s timeout, and
   the mock layer (`mocks/bin/impala-shell`, `mocks/scenarios/`) fakes it.
   Could it also run `EXPLAIN <job sql>` and `SHOW TABLE STATS <t>`? What do
   the mocks need to grow?
2. **Latency and UX cost** — the TUI must never block the event loop
   (`.agents/skills/dispatch-textual-tui/SKILL.md`); a 30s metadata call
   during form entry needs worker + spinner treatment. Is that acceptable in
   the New Job flow, or only behind an explicit "Analyze" action?
3. **Failure modes** — no Kerberos ticket, all pools busy, table not yet
   created (the Job itself creates it). The advisor must degrade to
   static-only analysis, not block composition.
4. **The static alternative** — the manual ships a fixed recommended
   join-strategy table (CORE/GCO/MRS tables). Is embedding that table as data
   enough for v1, deferring live stats entirely?

The answer decides whether the rule catalog's "needs-metadata" rules are in
or out of the v1 spec, and whether `EXPLAIN`-based verification is a v1
feature or fog for a later effort.

## Resolution

[The metadata research](../assets/metadata-availability-research.md) makes v1
**static-only**: needs-metadata rules are out of the v1 spec, and
`EXPLAIN`-based verification is deferred to a future effort. The existing
`impala.query()` plumbing could technically run `EXPLAIN` and
`SHOW TABLE STATS`, but the mock layer has no routing for either (probed —
they fall through to scenario dispatch), a 30-second-timeout call has no
place in the New Job screen's live validation loop, and composition must
never block on Kerberos or pool failures. The manual's fixed join-strategy
table is embedded as data instead, which covers the known-table hint rule
with zero metadata calls. If live metadata returns later, it belongs behind
an explicit Analyze action with worker/spinner/cancel treatment.

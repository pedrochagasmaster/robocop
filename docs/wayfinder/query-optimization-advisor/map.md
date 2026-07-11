---
label: wayfinder:map
status: open
---

# Map: Query Optimization Advisor

## Destination

A locked, hand-off-ready spec for a pre-launch **Query Optimization Advisor**
in Dispatch: which guidelines from the
[Impala optimization manual](assets/impala-optimization-manual.md) are checked
against a Job's SQL, how flag-only findings guide the user without mutating
that SQL, and where the analysis surfaces in the TUI. The map is done when
nothing is left to decide before implementation starts.

## Notes

- Domain: Dispatch, a server-side Textual TUI for launching Impala **Jobs**
  (see `CONTEXT.md` for the glossary — Job, Source, Destination, Orchestrator
  script, Resource Pool, Edge Node). Use that vocabulary in every ticket.
- The source guidelines live at
  [assets/impala-optimization-manual.md](assets/impala-optimization-manual.md)
  (uploaded "Hadoop Usage Guidelines: SQL Query Optimization Manual", v2.0).
- Initial preference from the sponsor: **automatic query update was the ideal;
  a rating/flagging system was the acceptable fallback**. The engine research
  established flag-only analysis as the safe v1 ceiling.
- Hard constraints every session must respect:
  - `scr/` orchestrators are effectively frozen
    ([ADR-0005](../../adr/0005-scr-modification-policy.md)); the advisor lives
    entirely in `dispatch/`.
  - Dispatch deploys to an air-gapped Edge Node via vendored wheels /
    edge-deploy-core bundles; new third-party dependencies must survive that
    path and Python >= 3.10.
  - Analysis must work offline against the mock layer (ADR-0004) — local dev
    has no Hadoop, Kerberos, or real `impala-shell`.
- Skills to consult: `.agents/skills/dispatch-textual-tui/SKILL.md` for any
  UI-facing ticket; `docs/agents/domain.md` before exploring.
- This effort is **planning only**: tickets resolve decisions, they do not
  build the advisor. Implementation starts after the spec is locked.
- **Tracker**: this repo's canonical tracker is GitHub issues
  (`docs/agents/issue-tracker.md`), but the charting session's `gh` access was
  read-only, so the map uses the local-markdown fallback: this file is the map
  issue, `tickets/*.md` are its child issues, front matter carries labels,
  claim (`assignee`), and native blocking (`blocked-by`). A session with `gh`
  write access may migrate map and tickets to GitHub issues verbatim
  (label `wayfinder:map`, tickets as linked children) and delete this
  directory. Wayfinding operations on this tracker:
  - **Claim**: set `assignee` in the ticket's front matter before working it.
  - **Close**: set `status: closed`, append a `## Resolution` section to the
    ticket, and add its line to Decisions so far.
  - **Frontier query**: tickets with `status: open`, `assignee: none`, and
    every `blocked-by` entry closed. Frontier at charting time:
    [Rule catalog](tickets/0001-rule-catalog.md),
    [SQL analysis engine research](tickets/0002-sql-analysis-engine-research.md),
    [Metadata availability research](tickets/0003-metadata-availability-research.md).
- Glossary gap: "Advisor" / "Finding" are new domain concepts not yet in
  `CONTEXT.md`; add them when the spec locks.

## Decisions so far

<!-- one line per closed ticket: gist + link -->

- [Research: SQL analysis engine options under the air-gapped deploy path](tickets/0002-sql-analysis-engine-research.md)
  — use SQLGlot with a length-preserving Impala adapter for flag-only v1
  analysis; never render or launch parser-generated SQL.
- [Research: what Impala metadata is available at pre-launch analysis time](tickets/0003-metadata-availability-research.md)
  — v1 analysis is static-only; embed the manual's join-strategy table as
  data, park needs-metadata rules, and defer EXPLAIN verification to a future
  Analyze action.
- [Rule catalog: which manual guidelines become machine-checkable rules](tickets/0001-rule-catalog.md)
  — eighteen rules locked (4 error / 6 warning / 8 info) with exact detection
  conditions; schemas, partition columns, and join strategies ship as data;
  SqlTemplate analyzed once, ExistingTable not analyzed.
- [Decide: remediation guidance for flag-only findings](tickets/0004-advisory-vs-rewrite.md)
  — rule-specific mix: diagnostic detection line always, imperative step only
  where deterministic, alternative-naming for author's-call rules; rule id +
  guideline reference on every finding.

## Not yet specified

- **Testing plan and mock scenarios** — pytest coverage for the analyzer and
  its Impala syntax corpus. Analysis is static-only, so no new `impala-shell`
  mock routing is needed; the corpus must cover the locked catalog's
  SQL-analysis rules (R01–R15, plus DDL entries for R17/R18 — R16 is a
  form-field check needing no SQL corpus).
- **Configuration and suppression UX** — per-user opt-out and per-rule
  suppression. Depends on catalog and surface decisions. (The join-strategy
  data file and its update procedure are now the
  [embedded join-strategy data file ticket](tickets/0008-join-strategy-data-file.md).)

## Out of scope

- **Live metadata checks (EXPLAIN, SHOW TABLE STATS) during composition** —
  the
  [metadata availability research](tickets/0003-metadata-availability-research.md)
  found no mock routing, unacceptable latency in live validation, and
  composition-blocking failure modes. A future effort may add them behind an
  explicit Analyze action with worker/spinner/cancel treatment.
- **Query rewriting and auto-fix mechanics** — the
  [SQL analysis engine research](tickets/0002-sql-analysis-engine-research.md)
  found no faithful Impala parse/render path. V1 never mutates or substitutes
  Job SQL; any future source-editing capability requires a fresh effort.
- **Resource/environment hygiene guidelines** (shut down kernels, max 3
  PySpark ports, quarterly file cleanup, JupyterHub process management) — they
  are not properties of a Job's SQL text and cannot be checked at launch time.
- **Post-run PROFILE feedback loop** (comparing estimated vs actual rows after
  a Job finishes) — valuable, but past this destination; would return as a
  fresh effort.
- **Orchestrator-side enforcement** — putting checks inside `scr/` scripts
  contradicts ADR-0005.
- **Cluster-side policy enforcement** (admission control, Sentry/Ranger
  rules) — outside Dispatch's reach entirely.

---
title: "Assemble and lock the Query Optimization Advisor spec"
labels: [wayfinder:task]
status: open
assignee: none
blocked-by:
  [
    0001-rule-catalog,
    0002-sql-analysis-engine-research,
    0003-metadata-availability-research,
    0004-advisory-vs-rewrite,
    0005-enforcement-policy,
    0006-tui-surface-prototype,
  ]
---

## Question

Fold every decision on this map into one implementation-ready spec document
(proposed home: `docs/query-optimization-advisor-spec.md`), plus the ADR for
the analysis engine dependency and Impala adapter boundary. This is the map's
destination.

The spec must cover, with nothing left open:

- the rule catalog (ids, detection conditions, severities, and manual
  remediation guidance)
- the analysis engine and any new dependency, with its vendored-wheel story
- flag-only behavior and the invariant that source and launched SQL remain
  unchanged
- launch enforcement policy and override paths
- the TUI surface and interaction flow, referencing the accepted prototype
- scoring/aggregation model, SqlTemplate/ExistingTable handling, config and
  suppression (all graduating from the map's fog before this ticket unblocks)
- testing expectations: pytest coverage for the analyzer and its syntax
  corpus, plus Edge-Node smoke items reviewers must check manually — no new
  `mocks/scenarios/` entries per the
  [metadata availability research](0003-metadata-availability-research.md)
- new `CONTEXT.md` glossary entries (Advisor, Finding, and whatever else the
  effort coined)

Resolved when the sponsor signs off on the document and it is merged. Any
disagreement discovered during assembly reopens the relevant ticket rather
than being settled ad hoc here.

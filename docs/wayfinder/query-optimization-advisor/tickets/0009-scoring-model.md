---
title: "Decide: scoring/aggregation model for findings"
labels: [wayfinder:grilling]
status: open
assignee: none
blocked-by: []
---

## Question

The sponsor's accepted fallback was "a rating or flagging system" — the
catalog delivers the flagging; does v1 also compute a **rating**, and if so,
how do eighteen findings roll up into one?

Grill through:

1. **No rating** — the findings list (with severity counts) is the product;
   a scalar adds noise. Simplest, and nothing in the locked catalog needs it.
2. **Worst-severity badge** — the Job is labeled by its worst finding
   (error / warning / info / clean). Trivially explainable, no arithmetic.
3. **Letter grade or numeric score** — weighted roll-up (e.g. error=X,
   warning=Y points). Familiar, but weights are arbitrary, and a "B" invites
   gaming ("how many warnings can I keep and still pass?").

Whatever is chosen must answer: does the rating change behavior anywhere
(sort order in lists, color in the action bar), or is it purely display?
Enforcement hooks stay with the
[enforcement policy ticket](0005-enforcement-policy.md) either way.

The catalog's severity semantics (confidence × impact) were not designed as
score weights; treat any numeric mapping with suspicion during grilling.

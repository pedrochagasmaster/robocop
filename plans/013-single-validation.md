# Plan 013: Collapse New Job's triple-redundant validation into one source of truth

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/screens/new_job.py`
> If `dispatch/screens/new_job.py` changed since this plan was written,
> compare the "Current state" excerpts against the live code before
> proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: plans/003-stop-sync-keystroke-walk.md (Plan 003 changes
  `_validation_issues`'s `can_launch` call; land 003 first so this plan
  refactors the already-fixed version)
- **Category**: tech-debt
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

`NewJobScreen` encodes the validation rules **three times**:
- `_inline_validate` (`new_job.py:290-311`) — builds the inline ✓/✗ indicator string.
- `_validation_issues` (`:313-341`) — builds the issue list for the summary count.
- `_validate` (`:461-492`) — returns the single blocking error for launch.

All three encode the same rules: legal Source/Destination cell, SQL file
exists, email shape, Kerberos TTL ≥ 300, `jobs.can_launch()` cap, SqlTemplate
date range, ExistingTable name required. The email regex is duplicated at
`:301`, `:333`, `:467`; the Kerberos `< 300` threshold at `:307`, `:339`,
`:425`, `:473`. They have already drifted: `_inline_validate` says "Kerberos
missing" while `_validation_issues` says "press K to kinit" — same rule, two
surfaces. Every future rule change must touch three places or the inline
indicator, the summary count, and the launch gate disagree.

## Current state

`dispatch/screens/new_job.py:290-311` (`_inline_validate`), `:313-341`
(`_validation_issues`), `:461-492` (`_validate`) — three functions over the
same inputs. Email regex at `:301`, `:333`, `:467`. Kerberos `< 300` at
`:307`, `:339`, `:425`, `:473`.

**Repo conventions**: the winner is `_validate` because it already returns
the gate decision. The refactor makes `_validate` return a list of
`(severity, message)` issues; `_inline_validate` and `_update_validation_summary`
derive from that same list. The launch gate is "non-empty issue list with any
`error` severity blocks".

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `dispatch/screens/new_job.py`

**Out of scope**:
- `dispatch/manifest.py` `LEGAL_CELLS` — the source of truth for the legal-cell
  rule; this plan consumes it, does not change it. (Converging the *other*
  scatter sites is Plan 013's sibling, the legal-cell consolidation — NOT
  this plan; this plan is validation-only.)
- `dispatch/screens/job_detail.py`, `dispatch/screens/browser.py` — unrelated.
- The prefill path (`_apply_prefill`) — it calls `_inline_validate` and
  `_update_validation_summary`; those calls still work post-refactor.

## Git workflow

- Branch: `advisor/013-single-validation`
- Commit per step; message style: `refactor(new_job): collapse triple validation into one source of truth`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add a single `_collect_issues` returning a list of (severity, message)

Add a function that returns all issues with severity, replacing the three
parallel implementations:

```python
def _email_is_valid(email: str) -> bool:
    return "@" in email and "." in email.split("@")[-1]

def _collect_issues(self) -> list[tuple[str, str]]:
    """Single source of truth for New Job validation.

    Returns a list of ``(severity, message)`` where severity is
    ``"error"`` (blocks launch), ``"warning"``, or ``"ok"`` (inline ✓).
    ``_validate`` returns the first ``error``; ``_inline_validate`` and
    ``_update_validation_summary`` derive their display from this list.
    """
    source = self._selected_source()
    destination = self._selected_destination()
    issues: list[tuple[str, str]] = []

    # Legal cell
    if (source, destination) not in manifest.LEGAL_CELLS:
        issues.append(("error", f"Illegal Source/Destination cell: {source}/{destination}"))

    # SQL file (SqlFile / SqlTemplate)
    if source in ("SqlFile", "SqlTemplate"):
        if not self._input_value("sql-file"):
            issues.append(("error", "SQL file path is required"))
        elif not self._sql_file_exists():
            issues.append(("error", "SQL file not found"))
        else:
            issues.append(("ok", "SQL file found"))

    # ExistingTable name
    if source == "ExistingTable" and not self._input_value("existing-table"):
        issues.append(("error", "Existing table name is required"))

    # SqlTemplate date range + completeness
    if source == "SqlTemplate":
        sql_text = self._read_sql()
        if sql_text is not None:
            if sql.is_malformed_template(sql_text):
                issues.append(("error", "SQL contains only one of {date_inicio}/{date_fim} — likely a typo"))
            elif not sql.template_is_complete(sql_text):
                issues.append(("error", "SqlTemplate requires both {date_inicio} and {date_fim}"))
        date_error = sql.validate_date_range(
            self._input_value("start-date"), self._input_value("end-date")
        )
        if date_error:
            issues.append(("error", date_error))

    # Email
    email = self._input_value("email")
    if email:
        if _email_is_valid(email):
            issues.append(("ok", "Email"))
        else:
            issues.append(("error", "Invalid email format"))

    # Concurrency cap (uses the cached count from Plan 003; if 003 not landed,
    # this calls jobs.can_launch() — acceptable on the validation path which
    # is not per-keystroke after 003)
    if self._running_count() >= jobs.RUNNING_CAP:
        issues.append(("error", f"At the {jobs.RUNNING_CAP}-Job concurrency cap"))

    # Kerberos
    if self.kerberos_ttl is None:
        issues.append(("error", "Kerberos missing — press K to kinit"))
    elif self.kerberos_ttl < 300:
        issues.append(("warning", "Kerberos TTL under 5 min — press K to renew"))
    else:
        issues.append(("ok", "Kerberos"))

    return issues
```

If Plan 003 has not landed, replace `self._running_count()` with
`jobs.can_launch()` and `not jobs.can_launch()` for the cap check. Prefer
landing 003 first (see Depends on).

### Step 2: Rewrite `_validate`, `_inline_validate`, `_update_validation_summary` to derive from `_collect_issues`

```python
def _validate(self) -> str | None:
    for severity, msg in self._collect_issues():
        if severity == "error":
            return msg
    return None

def _inline_validate(self) -> None:
    """Inline ✓/✗ indicators derived from the single issue list."""
    parts = []
    for severity, msg in self._collect_issues():
        if severity == "ok":
            parts.append(f"[green]\u2713[/] {msg}")
        elif severity == "warning":
            parts.append(f"[yellow]\u26a0[/] {msg}")
        elif severity == "error":
            # Inline indicator uses a short label, not the full message
            parts.append(f"[red]\u2717[/] {msg.split('—')[0].strip()}")
    self.query_one("#warning-text", Static).update("  ".join(parts))

def _update_validation_summary(self) -> None:
    issues = [i for i in self._collect_issues() if i[0] == "error"]
    summary = self.query_one("#validation-summary", Static)
    if issues:
        first = issues[0][1]
        extra = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
        summary.update(f"[red]\u2717 {len(issues)} issue(s): {first}{extra}[/]")
    else:
        summary.update("[green]\u2713 Ready to launch (checks passing)[/]")
```

Delete the old standalone `_validation_issues` function — its content is now
inside `_collect_issues`.

### Step 3: Verify the prefill path still works

`_apply_prefill` and `_force_radio` call `_inline_validate` and
`_update_validation_summary` (`new_job.py:732-733`). Those calls still work
because the function signatures are unchanged. Run the existing prefill tests
to confirm.

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 4: Add a regression test

In `tests/test_qa_fixes.py` or `tests/test_new_features.py`, add a test that
asserts the three surfaces agree: mount `NewJobScreen`, set an illegal cell,
and assert `_validate()` returns the error, the summary shows "1 issue(s)",
and the inline indicator shows a `✗`. Then set a valid cell and assert all
three go green. This guards against future drift back to three sources.

**Verify**: `python -m pytest tests -q` → all pass.

## Test plan

- New test: `test_validation_surfaces_agree` asserting `_validate`, the
  summary, and the inline indicator are consistent for an illegal cell and a
  valid cell.
- Structural pattern: existing New Job validation tests in
  `tests/test_qa_fixes.py:218-233` and `tests/test_new_features.py`.
- Verification: `python -m pytest tests -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests -q` exits 0; the new agreement test passes
- [ ] `grep -n "def _validation_issues" dispatch/screens/new_job.py` returns
      no matches (the old function is gone)
- [ ] `grep -n "def _collect_issues" dispatch/screens/new_job.py` returns a
      match
- [ ] The email regex appears exactly once in `new_job.py` (in
      `_email_is_valid`), not three times
- [ ] The Kerberos `< 300` threshold appears exactly once (in
      `_collect_issues`), not four times
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- The three functions at `:290-311`, `:313-341`, `:461-492` do not match the
  excerpts (the validation structure drifted — re-check before refactoring).
- A test in `tests/test_phase1_safety.py` or `tests/test_qa_fixes.py` asserts
  the exact text of `_validation_issues()` output and breaks — the new
  summary format must match the old ("N issue(s): first (+M more)"); STOP if
  it can't match without compromising the single-source design.
- `_read_sql()` is called inside `_collect_issues` for SqlTemplate and
  introduces a sync file read on the inline-validate path — if so, gate the
  SqlTemplate SQL-shape checks behind a "only when source is SqlTemplate"
  branch (the excerpt already does) and confirm it does not fire per keystroke
  for SqlFile. If it does, STOP and report.

## Maintenance notes

- The single-source design means a new validation rule is one append to
  `_collect_issues` and all three surfaces update. Document this in a
  one-line comment above `_collect_issues`.
- The `_email_is_valid` helper and the Kerberos threshold are now defined
  once. If the threshold moves from 300s, change it in `_collect_issues` only.
- The inline indicator splits on `—` to shorten the message; if a future
  issue message contains no `—`, the whole message shows, which is fine.
- Reviewer: confirm the prefill path (`_apply_prefill` → `_force_radio` →
  `_inline_validate`) still updates the indicator correctly after the
  refactor.

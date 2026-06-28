# Plan 014: Align the `config.json` email schema — installer writes it, TUI ignores it

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- install.sh dispatch/config.py dispatch/screens/new_job.py tests/test_install_onboarding.py`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: docs
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

`install.sh:76` writes `{"email": "<value>"}` to `config.json` (prompting for
`DISPATCH_EMAIL` or reading it interactively). `tests/test_install_onboarding.py:96-97`
asserts that shape. But the TUI **never reads** top-level `"email"`:
`dispatch/config.py:54-61` only exposes `form_defaults`; `NewJobScreen`
prefills email from `DISPATCH_EMAIL` env (`new_job.py:122-123`) or from
`form_defaults.email` on later visits (`new_job.py:747-761`). So the installer
and its tests document a schema the runtime ignores — users who set their
email at install time must re-enter it on first launch unless `form_defaults`
has it from a prior session.

## Current state

`install.sh:69-77` — writes `{"email": "<value>"}`:

```
69: CONFIG="$DISPATCH_HOME/config.json"
70: if [ ! -f "$CONFIG" ]; then
71:   EMAIL=${DISPATCH_EMAIL:-}
72:   if [ -z "$EMAIL" ]; then
73:     printf "Email: "
74:     read -r EMAIL
75:   fi
76:   printf '{\n  "email": "%s"\n}\n' "$EMAIL" >"$CONFIG"
77: fi
```

`dispatch/config.py:54-61` — `read_form_defaults` reads only
`cfg.get("form_defaults", {})`; no reader of top-level `"email"` exists.

`dispatch/screens/new_job.py:122-123` — email Input prefills from
`DISPATCH_EMAIL` env, not from `config.json["email"]`.

`dispatch/screens/new_job.py:747-761` — `_apply_saved_defaults` reads
`form_defaults` (schema: `{"schema": ..., "email": ..., "subject": ...,
"destination_type": ...}`), not top-level `"email"`.

`tests/test_install_onboarding.py:96-97` — asserts `{"email": ...}`.

**Repo conventions**: `form_defaults` is the canonical store for last-used
form values, written by `_save_form_defaults` (`new_job.py:763-773`) on every
launch. The cleanest fix is to make the installer write the email into
`form_defaults` (matching the runtime's actual schema) so a first-time user
sees their email pre-filled, and to update the test to match.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `install.sh` — write `form_defaults.email` instead of top-level `email`.
- `tests/test_install_onboarding.py` — update the asserted schema.
- `docs/edge-node-first-time-setup.md` and `onboarding.md` — if they document
  the `config.json` email field, update them to match.

**Out of scope**:
- `dispatch/config.py` — `read_form_defaults` already reads the right shape;
  no change needed.
- `dispatch/screens/new_job.py` — already reads `form_defaults.email` via
  `_apply_saved_defaults`; no change needed.
- Migrating existing edge installs' `config.json` from `{"email": ...}` to
  `{"form_defaults": {"email": ...}}` — the runtime tolerates both (it
  ignores top-level `email`), so old installs simply lose the pre-fill until
  the user re-enters. A migration is optional and out of scope; note it in
  maintenance notes.

## Git workflow

- Branch: `advisor/014-config-email-schema`
- Commit per step; message style: `fix(install): write email into form_defaults schema the TUI reads`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Change `install.sh` to write `form_defaults.email`

Replace `install.sh:76`:

```sh
  printf '{\n  "form_defaults": {\n    "email": "%s"\n  }\n}\n' "$EMAIL" >"$CONFIG"
```

Keep the `if [ ! -f "$CONFIG" ]` guard so re-runs don't overwrite an existing
config (the installer is idempotent per README).

### Step 2: Update the installer test

In `tests/test_install_onboarding.py:96-97`, change the asserted schema to
match. The test likely reads the written `config.json` and asserts a key;
update it to assert `data["form_defaults"]["email"] == "<expected>"` instead
of `data["email"]`. Read the test first to confirm its exact assertion shape.

### Step 3: Update onboarding docs if they mention the email field

Check `onboarding.md` and `docs/edge-node-first-time-setup.md` for references
to the `config.json` email field. If they tell users to edit
`config.json`'s top-level `"email"`, update them to point at
`form_defaults.email` (or, better, tell users to set `DISPATCH_EMAIL` at
install time and let the TUI's "save form defaults on launch" handle
persistence).

**Verify**: `python -m compileall dispatch scr` → exit 0;
`python -m pytest tests/test_install_onboarding.py -q` → all pass.

## Test plan

- Update the existing installer test in `tests/test_install_onboarding.py`
  to assert the new `form_defaults.email` schema.
- Optionally add a test asserting `NewJobScreen._apply_saved_defaults`
  populates the email Input from `form_defaults.email` (if not already
  covered by `tests/test_qa_fixes.py` or `tests/test_new_features.py`).
- Verification: `python -m pytest tests -q` → all pass.

## Done criteria

- [ ] `python -m pytest tests/test_install_onboarding.py -q` exits 0 with
      the updated assertion
- [ ] `grep -n '"email":' install.sh` returns no matches (top-level email
      removed); `grep -n '"form_defaults"' install.sh` returns a match
- [ ] `python -m pytest tests -q` exits 0 (no regressions)
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `install.sh:76` no longer matches the excerpt (the installer was
  restructured — re-check before patching).
- `tests/test_install_onboarding.py:96-97` asserts something other than the
  `{"email": ...}` shape (the test was already updated — reconcile).
- A downstream consumer (e.g. `tools/prod_tui/` or a doc) reads
  `config.json["email"]` directly — grep `["email"]` and `['email']` across
  the repo; if a real consumer exists, STOP and report (the fix must update
  that consumer too, or the schema change breaks it).
- Existing edge installs rely on top-level `email` and a migration is
  required — STOP and report; a migration shim in `config.read_config` would
  be a separate, larger change.

## Maintenance notes

- Old edge installs with `{"email": "..."}` will silently lose the email
  pre-fill until the user re-enters it (the runtime ignores top-level
  `email`). If this is unacceptable, add a one-time migration in
  `config.read_form_defaults` that lifts `cfg["email"]` into
  `cfg["form_defaults"]["email"]` if `form_defaults` lacks it — but that's a
  separate plan; this plan only fixes the installer so new installs are
  correct.
- The `form_defaults` schema also carries `schema`, `subject`, and
  `destination_type` (per `_save_form_defaults`). The installer only seeds
  `email`; the others fill in on first launch. That's the intended behavior.
- Reviewer: confirm a fresh install (with `DISPATCH_EMAIL` set) results in
  the New Job form's email field pre-filled on first open.

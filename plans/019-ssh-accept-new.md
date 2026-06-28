# Plan 019: Harden SSH options in the prod-test harness (`accept-new` not `no`)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- tools/prod_tui/config.yaml tools/prod_tui/config-node04.yaml tools/prod_tui/tests/test_preflight.py tools/prod_tui/tests/test_tmux_commands.py tools/prod_tui/README.md docs/edge-node-tui-operating-model.md`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

The prod-test harness configs bake `StrictHostKeyChecking=no` into SSH
options (`tools/prod_tui/config.yaml:6`, `config-node04.yaml:6`). The deploy
script already uses the safer `accept-new` (`deploy_nodes_03_04.ps1:66`), so
there's an inconsistency. `StrictHostKeyChecking=no` silently accepts unknown
host keys, which is a TOFU weakness; `accept-new` accepts a key only on first
connection and pins it thereafter, which is the safer default for connecting
to known corporate edge nodes (node03/node04). This is internal tooling (the
harness, not the shipped TUI), so leverage is lower than product findings,
but the fix is trivial and aligns with the deploy script's existing practice.

## Current state

`tools/prod_tui/config.yaml:6`:

```
ssh_options: "-p 2222 -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=1000"
```

`tools/prod_tui/config-node04.yaml:6` — identical.

`deploy_nodes_03_04.ps1:66` — uses `accept-new`:

```
ssh -p $RemotePort -o StrictHostKeyChecking=accept-new $remoteTarget ...
```

Tests assert the `no` value: `tools/prod_tui/tests/test_preflight.py:17`,
`tools/prod_tui/tests/test_tmux_commands.py:36,39`, and
`tools/prod_tui/tests/test_cli.py:93` references it in a README assertion.

`docs/edge-node-tui-operating-model.md:300` and `tools/prod_tui/README.md:34,42`
document the `no` value.

**Repo conventions**: the harness is YAML-config-driven
(`tools/prod_tui/config*.yaml`); tests assert the exact `ssh_options` string.
Changing the value requires updating the tests and docs in lockstep.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Tests     | `python -m pytest tools/prod_tui/tests -q` | all pass            |

## Scope

**In scope**:
- `tools/prod_tui/config.yaml` and `tools/prod_tui/config-node04.yaml` —
  change `StrictHostKeyChecking=no` to `accept-new`.
- `tools/prod_tui/tests/test_preflight.py`,
  `tools/prod_tui/tests/test_tmux_commands.py`,
  `tools/prod_tui/tests/test_cli.py` — update assertions.
- `tools/prod_tui/README.md`, `docs/edge-node-tui-operating-model.md` —
  update documented examples.

**Out of scope**:
- `deploy_nodes_03_04.ps1` — already uses `accept-new`; no change.
- The shipped `dispatch/` TUI — this is harness-only; the product does not
  make SSH connections.
- Removing `ServerAliveInterval`/`ServerAliveCountMax` — those are
  intentional keepalive settings for long-running tmux sessions; keep them.

## Git workflow

- Branch: `advisor/019-ssh-accept-new`
- Commit per step; message style: `security(harness): use StrictHostKeyChecking=accept-new instead of no`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Change both harness configs

In `tools/prod_tui/config.yaml:6` and `tools/prod_tui/config-node04.yaml:6`,
replace `StrictHostKeyChecking=no` with `StrictHostKeyChecking=accept-new`:

```yaml
ssh_options: "-p 2222 -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=1000"
```

### Step 2: Update the tests that assert the `no` value

In `tools/prod_tui/tests/test_preflight.py:17`, change the asserted
`ssh_options` to the new `accept-new` string.

In `tools/prod_tui/tests/test_tmux_commands.py:36,39`, change the
`ssh_options` fixture and the `assert pane_cmd.startswith(...)` expectation
to the new string.

In `tools/prod_tui/tests/test_cli.py:93`, update the README-assertion string.

Read each test first to confirm the exact assertion shape before editing.

### Step 3: Update docs

In `tools/prod_tui/README.md:34,42` and
`docs/edge-node-tui-operating-model.md:300`, replace
`StrictHostKeyChecking=no` with `StrictHostKeyChecking=accept-new` in the
documented examples. Keep the `README.md:42` line that explains
`ssh_options` accepts normal OpenSSH options.

**Verify**: `python -m pytest tools/prod_tui/tests -q` → all pass.

## Test plan

- Update the three existing tests that assert the `no` value; no new tests
  needed (the existing assertions pin the new value).
- Verification: `python -m pytest tools/prod_tui/tests -q` → all pass.

## Done criteria

- [ ] `grep -rn "StrictHostKeyChecking=no" tools/prod_tui docs` returns no
      matches (except possibly archive docs under `docs/archive/`)
- [ ] `grep -rn "StrictHostKeyChecking=accept-new" tools/prod_tui/config*.yaml` returns matches in both files
- [ ] `python -m pytest tools/prod_tui/tests -q` exits 0
- [ ] `python -m pytest tests -q` exits 0 (no regressions in the main suite)
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- The harness tests at `test_preflight.py:17`/`test_tmux_commands.py:36,39`
  assert a different `ssh_options` shape than the excerpt (the config or
  tests were restructured — re-check before editing).
- A harness test fails after the change for a reason other than the asserted
  string (e.g. the harness actually parses `ssh_options` and rejects
  `accept-new`) — STOP and report; the harness parser may need updating.
- `deploy_nodes_03_04.ps1:66` no longer uses `accept-new` (the deploy script
  reverted to `no` — reconcile rather than diverge again).

## Maintenance notes

- `accept-new` is the right default for connecting to known corporate edge
  nodes: it pins the host key on first connection and fails if it changes
  later (detecting MITM), while avoiding the interactive prompt on first
  use. Do NOT revert to `no`.
- If the edge-node host keys are ever regenerated (e.g. node rebuild), the
  harness will fail with a host-key mismatch — that's the desired behavior
  (alert the operator), not a bug. Document this in the README if it's not
  already clear.
- Reviewer: confirm the `tools/prod_tui/tests` suite passes with the new
  string and that no archive doc under `docs/archive/` is updated (archive
  docs are historical).

# Dispatch QA matrix

This folder holds the single canonical feature/user-story matrix for Dispatch
and the QA closure loop run against it.

## Files

- `build_feature_matrix.py` — source of truth. Each `ROWS` entry is one
  discrete, testable user-facing behavior derived from the code.
- `feature-user-stories.csv` — rendered spreadsheet (do not hand-edit; edit the
  generator and re-render with `python docs/qa/build_feature_matrix.py`).

## Columns

| Column | Meaning |
|---|---|
| `id` | Stable identifier `AREA-NN` |
| `area` | Feature area (App Shell, Overview, New Job, …) |
| `feature` | Short feature name |
| `user_story` | `As a <role>, I want <goal> so that <benefit>` |
| `expected` | Precise, observable expected behavior |
| `source_refs` | `file:symbol` / `file:line` backing the behavior |
| `status` | `Documented` → `Tested` → `Fixed` → `Verified` |
| `test_method` | pytest node id, Textual pilot test, or manual step |
| `test_result` | `PASS` / `FAIL` / `N/A` from the first test pass |
| `errors_found` | Logistical/UX errors discovered while testing |
| `fix_applied` | Fix made during the fix phase |
| `retest_result` | `PASS` / `FAIL` from the post-fix re-test |

## Loop

1. **Document** — enumerate every feature as a user story with expected
   behavior (status `Documented`).
2. **Test** — exercise every story (existing pytest suite + targeted Textual
   pilot tests + manual GUI runs over the mock layer). Record `test_method`,
   `test_result`, and any `errors_found` (status `Tested`).
3. **Fix** — fix every logistical or UX error found (status `Fixed`).
4. **Re-test** — re-exercise every story affected by a fix and confirm the
   regression tests pass (status `Verified`).

Regeneration is reproducible: `python docs/qa/build_feature_matrix.py`.

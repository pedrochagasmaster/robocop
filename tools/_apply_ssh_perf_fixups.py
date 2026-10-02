from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"pattern not found in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# The SQL-file picker assigns the Input value, which already schedules the
# debounced worker through Input.Changed. Do not do a second synchronous read.
replace(
    "dispatch/screens/new_job.py",
    '''        if sql_input.value != path:
            sql_input.value = path
            self._detect_sql()
''',
    '''        if sql_input.value != path:
            sql_input.value = path
''',
)

print("performance fixups applied")

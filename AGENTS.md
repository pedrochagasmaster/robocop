# AGENTS.md

## Cursor Cloud specific instructions

### Codebase overview

This is the **Hadoop Query Launcher** — a Windows GUI tool (PowerShell + Windows Forms) for executing SQL queries on a remote Apache Impala/Hadoop cluster. It consists of:

| Component | Language | Runs on |
|---|---|---|
| `run_query_engine.bat` | Batch | Windows (launcher) |
| `run_query.ps1` | PowerShell 5.1+ | Windows (GUI) |
| `scr/Query_Impala_Parametrized.py` | Python 3.10+ | Remote Hadoop edge node |
| `scr/download_to_csv.py` | Python 3.10+ | Remote Hadoop edge node |
| `scr/monthly_query_processor.py` | Python 3.10+ | Remote Hadoop edge node |

All Python scripts use **only standard library modules** — no `requirements.txt` or third-party packages exist.

### Development environment notes

- **The PowerShell GUI (`run_query.ps1`) cannot run on Linux** — it uses `System.Windows.Forms` and Win32 API calls (`user32.dll`). On Linux, you can only validate its syntax via `pwsh` (PowerShell Core):
  ```bash
  pwsh -Command '[System.Management.Automation.Language.Parser]::ParseFile("run_query.ps1", [ref]$null, [ref]$errors) | Out-Null; $errors'
  ```
- **Python scripts are designed for a remote Hadoop server**, not local execution. They depend on `impala-shell` CLI and Kerberos auth. Locally, you can validate syntax and run linting.

### Linting

```bash
# Python linting (flake8)
flake8 scr/ --max-line-length=120

# Python linting (pylint)
pylint scr/*.py --disable=C0114,C0115,C0116,C0103,W0718 --max-line-length=120

# PowerShell syntax check (pwsh must be installed)
pwsh -Command '$e=$null; [System.Management.Automation.Language.Parser]::ParseFile("run_query.ps1",[ref]$null,[ref]$e)|Out-Null; if($e.Count -gt 0){$e}'
```

### Testing

There are no automated tests in this repository. Validation is limited to:
1. Syntax checks: `python3 -m py_compile scr/<script>.py`
2. Linting (see above)
3. `--help` flag on each Python script to verify argument parsing works

### Important caveats

- `monthly_query_processor.py` imports from `Query_Impala_Parametrized.py` — they must reside in the same directory on the remote server.
- The Python scripts will fail at runtime without `impala-shell` and Kerberos — this is expected in the cloud dev environment.
- No build step is required — all scripts are interpreted directly.

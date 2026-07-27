"""Tests for the non-interactive ``dispatch job`` CLI and shared job_ops seam."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest

from dispatch import cli_job, config, job_ops, kerberos, manifest, process, telemetry
from dispatch.advisor.models import AnalysisResult, Finding
from dispatch.cli_job import (
    EXIT_JOB_UNSUCCESSFUL,
    EXIT_OK,
    EXIT_OPERATIONAL,
    EXIT_UNKNOWN_JOB,
    EXIT_USAGE,
)


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str


def _invoke(argv: list[str], *, monkeypatch, cwd: Path | None = None) -> CliResult:
    """Run ``dispatch job …`` in-process so monkeypatches apply."""
    if cwd is not None:
        monkeypatch.chdir(cwd)
    parser = argparse.ArgumentParser(prog="dispatch")
    subparsers = parser.add_subparsers(dest="command")
    cli_job.add_job_parser(subparsers)
    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            args = parser.parse_args(argv)
            if args.command != "job":
                raise AssertionError(f"expected job command, got {args.command!r}")
            code = cli_job.run_job_command(args)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    return CliResult(code, out.getvalue(), err.getvalue())


def _write_sql(path: Path, text: str = "SELECT 1\n") -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _seed_job(
    tmp_path: Path,
    *,
    state: str = "Pending",
    pid: int | None = None,
    exit_code: int | None = None,
    sql_name: str = "query.sql",
) -> Path:
    launch_cwd = tmp_path / "launch"
    launch_cwd.mkdir(exist_ok=True)
    sql_path = _write_sql(launch_cwd / sql_name)
    job_dir, _item = manifest.create_job(
        source={"type": "SqlFile", "sql_path_at_launch": str(sql_path)},
        destination={
            "type": "Csv",
            "schema": "aa_enc",
            "table_name": "dispatch_result",
            "csv_path": str(launch_cwd / "dispatch_result.csv"),
        },
        params={"to_email": "", "subject": "t", "queue": "auto"},
        launch_cwd=launch_cwd,
        sql_text="SELECT 1",
    )
    updates: dict = {"state": state}
    if pid is not None:
        updates["pid"] = pid
    if exit_code is not None:
        updates["exit_code"] = exit_code
    if state in {"Succeeded", "Failed", "Cancelled"}:
        updates.setdefault("finished_at", manifest.now_utc())
        updates.setdefault("started_at", manifest.now_utc())
    manifest.update(job_dir / "manifest.json", **updates)
    return job_dir


def test_dispatch_help_lists_job_and_telemetry(mock_env) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dispatch", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "job" in result.stdout
    assert "telemetry" in result.stdout


def test_job_help_lists_subcommands(mock_env) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dispatch", "job", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    for name in ("launch", "list", "show", "logs", "wait", "cancel"):
        assert name in result.stdout


def test_python_m_dispatch_module_help_subprocess(mock_env) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dispatch", "job", "launch", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--source" in result.stdout
    assert "--acknowledge-advisor" in result.stdout


def test_launch_usage_error_without_required_flags(mock_env, monkeypatch) -> None:
    result = _invoke(["job", "launch"], monkeypatch=monkeypatch)
    assert result.returncode == 2


def test_launch_all_legal_cells(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)
    monkeypatch.setattr(process, "launch_runner_detached", lambda job_dir: 4242)

    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    sql_file = _write_sql(launch_cwd / "q.sql")
    template = _write_sql(
        launch_cwd / "monthly.sql",
        "select * from t where d between '{date_inicio}' and '{date_fim}'\n",
    )

    cells = [
        ["--source", "SqlFile", "--destination", "Csv", "--sql", sql_file.name, "--table", "c1"],
        [
            "--source",
            "SqlFile",
            "--destination",
            "Table",
            "--sql",
            sql_file.name,
            "--table",
            "t1",
            "--schema",
            "aa_enc",
        ],
        [
            "--source",
            "SqlFile",
            "--destination",
            "Table+Csv",
            "--sql",
            sql_file.name,
            "--table",
            "tc1",
            "--schema",
            "aa_enc",
        ],
        [
            "--source",
            "SqlTemplate",
            "--destination",
            "Table",
            "--sql",
            template.name,
            "--table",
            "m1",
            "--schema",
            "aa_enc",
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-01-31",
        ],
        [
            "--source",
            "ExistingTable",
            "--destination",
            "Csv",
            "--existing-table",
            "aa_enc.events_existing",
        ],
    ]
    for args in cells:
        result = _invoke(
            ["job", "launch", *args, "--yes"],
            monkeypatch=monkeypatch,
            cwd=launch_cwd,
        )
        assert result.returncode == EXIT_OK, (args, result.stderr, result.stdout)
        job_id = result.stdout.strip().splitlines()[-1]
        manifest_path = config.jobs_dir() / job_id / "manifest.json"
        assert manifest_path.is_file()
        manifest.update(
            manifest_path,
            state="Succeeded",
            exit_code=0,
            finished_at=manifest.now_utc(),
        )


def test_launch_rejects_illegal_cell(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    _write_sql(launch_cwd / "q.sql")
    result = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlTemplate",
            "--destination",
            "Csv",
            "--sql",
            "q.sql",
            "--yes",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert result.returncode == EXIT_USAGE
    assert "Illegal combination" in result.stderr


def test_launch_resolves_relative_sql_against_cwd(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)
    monkeypatch.setattr(process, "launch_runner_detached", lambda job_dir: 99)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    _write_sql(launch_cwd / "rel.sql")
    result = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--sql",
            "rel.sql",
            "--table",
            "rel_out",
            "--yes",
            "--json",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert result.returncode == EXIT_OK
    payload = json.loads(result.stdout)
    item = manifest.load(config.jobs_dir() / payload["job_id"] / "manifest.json")
    assert Path(item["source"]["sql_path_at_launch"]) == launch_cwd / "rel.sql"
    assert Path(item["destination"]["csv_path"]).parent == launch_cwd.resolve()


def test_launch_requires_yes(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    _write_sql(launch_cwd / "q.sql")
    result = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--sql",
            "q.sql",
            "--json",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert result.returncode == EXIT_USAGE
    err = json.loads(result.stderr)
    assert "yes" in err["error"].lower()


def test_launch_kerberos_refusal(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: None)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    _write_sql(launch_cwd / "q.sql")
    result = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--sql",
            "q.sql",
            "--yes",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert result.returncode == EXIT_OPERATIONAL
    assert "Kerberos" in result.stderr
    assert telemetry.flush(timeout=1)
    events = [
        json.loads(line)
        for line in telemetry.private_events_path().read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        e["event"] == "launch_refused" and e["props"]["reason"] == "kerberos" for e in events
    )


def test_launch_capacity_refusal(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)
    monkeypatch.setattr(process, "launch_runner_detached", lambda job_dir: 1)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    _write_sql(launch_cwd / "q.sql")
    for idx in range(2):
        _seed_job(tmp_path, state="Running", pid=os.getpid(), sql_name=f"cap{idx}.sql")
    result = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--sql",
            "q.sql",
            "--table",
            "cap_block",
            "--yes",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert result.returncode == EXIT_OPERATIONAL


def test_launch_handoff_failure_marks_failed(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)

    def boom(_job_dir: Path) -> int:
        raise OSError("nohup unavailable")

    monkeypatch.setattr(process, "launch_runner_detached", boom)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    _write_sql(launch_cwd / "q.sql")
    result = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--sql",
            "q.sql",
            "--table",
            "handoff_fail",
            "--yes",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert result.returncode == EXIT_OPERATIONAL
    assert "detached runner" in result.stderr.lower() or "nohup" in result.stderr.lower()
    manifests = list(config.jobs_dir().glob("*/manifest.json"))
    assert len(manifests) == 1
    item = manifest.load(manifests[0])
    assert item["state"] == "Failed"
    assert item["exit_code"] == -1
    assert telemetry.flush(timeout=1)
    events = [
        json.loads(line)
        for line in telemetry.private_events_path().read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["event"] == "job_launched" for e in events)


def test_launch_json_stdout_is_plain(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)
    monkeypatch.setattr(process, "launch_runner_detached", lambda job_dir: 7)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    _write_sql(launch_cwd / "q.sql")
    result = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--sql",
            "q.sql",
            "--table",
            "json_out",
            "--yes",
            "--json",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert result.returncode == EXIT_OK
    payload = json.loads(result.stdout)
    assert set(payload) >= {"job_id", "state", "pid"}
    assert "[" not in result.stdout


def test_list_and_show_reconcile_and_json(mock_env, tmp_path: Path, monkeypatch) -> None:
    job_dir = _seed_job(tmp_path, state="Running", pid=999_999)
    monkeypatch.setattr("dispatch.jobs.pid_is_alive", lambda pid: False)

    listed = _invoke(["job", "list", "--json"], monkeypatch=monkeypatch)
    assert listed.returncode == EXIT_OK
    payload = json.loads(listed.stdout)
    assert payload["jobs"]
    assert payload["jobs"][0]["id"] == job_dir.name
    assert payload["jobs"][0]["state"] == "Failed"

    shown = _invoke(["job", "show", job_dir.name, "--json"], monkeypatch=monkeypatch)
    assert shown.returncode == EXIT_OK
    detail = json.loads(shown.stdout)
    assert detail["state"] == "Failed"
    assert detail["manifest"]["id"] == job_dir.name


def test_list_state_filter_and_human(mock_env, tmp_path: Path, monkeypatch) -> None:
    _seed_job(tmp_path, state="Succeeded", exit_code=0, sql_name="a.sql")
    _seed_job(tmp_path, state="Failed", exit_code=1, sql_name="b.sql")
    result = _invoke(["job", "list", "--state", "Succeeded"], monkeypatch=monkeypatch)
    assert result.returncode == EXIT_OK
    assert "Succeeded" in result.stdout
    assert "Failed" not in result.stdout


def test_logs_tail_and_follow_stops_when_terminal(mock_env, tmp_path: Path, monkeypatch) -> None:
    job_dir = _seed_job(tmp_path, state="Running", pid=os.getpid())
    log_path = job_dir / "run.log"
    log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")

    tailed = _invoke(
        ["job", "logs", job_dir.name, "--lines", "2"],
        monkeypatch=monkeypatch,
    )
    assert tailed.returncode == EXIT_OK
    assert tailed.stdout.splitlines() == ["line2", "line3"]

    def finish_soon() -> None:
        time.sleep(0.2)
        manifest.update(
            job_dir / "manifest.json",
            state="Succeeded",
            exit_code=0,
            finished_at=manifest.now_utc(),
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("line4\n")

    threading.Thread(target=finish_soon, daemon=True).start()
    followed = _invoke(
        ["job", "logs", job_dir.name, "--lines", "1", "--follow"],
        monkeypatch=monkeypatch,
    )
    assert followed.returncode == EXIT_OK
    assert "line3" in followed.stdout
    assert "line4" in followed.stdout


def test_wait_success_failure_timeout(mock_env, tmp_path: Path, monkeypatch) -> None:
    ok = _seed_job(tmp_path, state="Succeeded", exit_code=0, sql_name="ok.sql")
    bad = _seed_job(tmp_path, state="Failed", exit_code=7, sql_name="bad.sql")
    cancelled = _seed_job(tmp_path, state="Cancelled", exit_code=0, sql_name="can.sql")
    pending = _seed_job(tmp_path, state="Pending", sql_name="pend.sql")

    assert (
        _invoke(["job", "wait", ok.name, "--json"], monkeypatch=monkeypatch).returncode == EXIT_OK
    )
    failed = _invoke(["job", "wait", bad.name, "--json"], monkeypatch=monkeypatch)
    assert failed.returncode == EXIT_JOB_UNSUCCESSFUL
    cancelled_wait = _invoke(["job", "wait", cancelled.name, "--json"], monkeypatch=monkeypatch)
    assert cancelled_wait.returncode == EXIT_JOB_UNSUCCESSFUL
    timed = _invoke(
        ["job", "wait", pending.name, "--timeout", "0.1", "--poll-interval", "0.05", "--json"],
        monkeypatch=monkeypatch,
    )
    assert timed.returncode == EXIT_OPERATIONAL
    payload = json.loads(timed.stdout)
    assert payload["timed_out"] is True


def test_invalid_queue_is_usage_error(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    _write_sql(launch_cwd / "q.sql")
    result = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--sql",
            "q.sql",
            "--queue",
            "not_a_pool",
            "--yes",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert result.returncode == EXIT_USAGE
    assert "Resource Pool" in result.stderr


def test_empty_subject_preserved_in_plan(mock_env, tmp_path: Path) -> None:
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    sql_path = _write_sql(launch_cwd / "q.sql")
    inputs = job_ops.LaunchInputs(
        source_type="SqlFile",
        destination_type="Csv",
        launch_cwd=launch_cwd,
        sql_path=str(sql_path),
        table_name=job_ops.table_name_for_inputs(
            source_type="SqlFile",
            destination_type="Csv",
            table_suffix_or_full="subj",
        ),
        subject="   ",
    )
    plan = job_ops.prepare_launch(inputs, kerberos_ttl=3600)
    assert plan.params["subject"] == ""


def test_launch_job_keeps_prepared_sql_after_recheck(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)
    monkeypatch.setattr(process, "launch_runner_detached", lambda job_dir: 1)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    sql_path = _write_sql(launch_cwd / "q.sql", "SELECT 1 AS kept\n")
    inputs = job_ops.LaunchInputs(
        source_type="SqlFile",
        destination_type="Csv",
        launch_cwd=launch_cwd,
        sql_path=str(sql_path),
        table_name=job_ops.table_name_for_inputs(
            source_type="SqlFile",
            destination_type="Csv",
            table_suffix_or_full="keep_sql",
        ),
    )
    seen: list[str] = []

    def on_plan(plan: job_ops.LaunchPlan) -> None:
        seen.append(plan.sql_text)
        sql_path.write_text("SELECT 2 AS changed\n", encoding="utf-8")

    result = job_ops.launch_job(
        inputs,
        kerberos_ttl=3600,
        yes=True,
        acknowledge_advisor=True,
        recheck_ttl=lambda: 3600,
        on_plan=on_plan,
    )
    assert result.job_id
    assert seen == ["SELECT 1 AS kept\n"]
    # Manifest job.sql should still be the prepared text, not the rewritten file.
    job_sql = (config.jobs_dir() / result.job_id / "job.sql").read_text(encoding="utf-8")
    assert "kept" in job_sql
    assert "changed" not in job_sql


def test_follow_logs_does_not_drop_lines_appended_during_initial_tail(
    mock_env, tmp_path: Path, monkeypatch
) -> None:
    job_dir = _seed_job(tmp_path, state="Succeeded", exit_code=0)
    log_path = job_dir / "run.log"
    log_path.write_text("a\nb\nc\n", encoding="utf-8")
    original = job_ops.read_log_tail_with_offset

    def racey_tail(job_id: str, *, lines: int = 50, root=None):
        text_lines, offset = original(job_id, lines=lines, root=root)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("d\n")
        return text_lines, offset

    monkeypatch.setattr(job_ops, "read_log_tail_with_offset", racey_tail)
    lines = list(job_ops.follow_logs(job_dir.name, lines=2, poll_interval=0.01))
    assert "b" in lines and "c" in lines and "d" in lines


def test_cancel_pending_and_running(mock_env, tmp_path: Path, monkeypatch) -> None:
    pending = _seed_job(tmp_path, state="Pending", sql_name="p.sql")
    running = _seed_job(tmp_path, state="Running", pid=4242, sql_name="r.sql")
    calls: list[int] = []
    monkeypatch.setattr(
        process, "cancel_process_group", lambda pid: calls.append(pid) or "signaled"
    )

    no_yes = _invoke(["job", "cancel", pending.name], monkeypatch=monkeypatch)
    assert no_yes.returncode == 2

    cancelled = _invoke(
        ["job", "cancel", pending.name, "--yes", "--json"],
        monkeypatch=monkeypatch,
    )
    assert cancelled.returncode == EXIT_OK
    body = json.loads(cancelled.stdout)
    assert body["result"] == "pending_cancelled"
    assert manifest.load(pending / "manifest.json")["state"] == "Cancelled"

    signaled = _invoke(
        ["job", "cancel", running.name, "--yes", "--json"],
        monkeypatch=monkeypatch,
    )
    assert signaled.returncode == EXIT_OK
    assert json.loads(signaled.stdout)["result"] == "signaled"
    assert calls == [4242]
    assert telemetry.flush(timeout=1)


def test_cancel_missing_pid_reconciles(mock_env, tmp_path: Path, monkeypatch) -> None:
    running = _seed_job(tmp_path, state="Running", pid=123456, sql_name="m.sql")
    monkeypatch.setattr(
        process,
        "cancel_process_group",
        lambda pid: (_ for _ in ()).throw(ProcessLookupError(pid)),
    )
    monkeypatch.setattr("dispatch.jobs.pid_is_alive", lambda pid: False)
    result = _invoke(
        ["job", "cancel", running.name, "--yes", "--json"],
        monkeypatch=monkeypatch,
    )
    assert result.returncode == EXIT_OPERATIONAL
    assert manifest.load(running / "manifest.json")["state"] == "Failed"


def test_unknown_and_unsafe_job_paths(mock_env, monkeypatch) -> None:
    missing = _invoke(["job", "show", "20990101T000000Z_missing"], monkeypatch=monkeypatch)
    assert missing.returncode == EXIT_UNKNOWN_JOB

    malformed = _invoke(["job", "show", "../etc/passwd"], monkeypatch=monkeypatch)
    assert malformed.returncode == EXIT_UNKNOWN_JOB

    jobs_root = config.jobs_dir()
    jobs_root.mkdir(parents=True, exist_ok=True)
    real = jobs_root / "20990101T000000Z_symlink"
    real.mkdir()
    (real / "manifest.json").write_text("{}", encoding="utf-8")
    link_name = "20990101T000001Z_linked"
    link = jobs_root / link_name
    if hasattr(os, "symlink"):
        try:
            os.symlink(real, link)
        except OSError:
            pytest.skip("symlinks unavailable")
        unsafe = _invoke(["job", "show", link_name], monkeypatch=monkeypatch)
        assert unsafe.returncode == EXIT_UNKNOWN_JOB


def test_corrupt_manifest_is_operational(mock_env, monkeypatch) -> None:
    jobs_root = config.jobs_dir()
    job_id = "20990101T010101Z_corrupt"
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "manifest.json").write_text("{not-json", encoding="utf-8")
    result = _invoke(["job", "show", job_id], monkeypatch=monkeypatch)
    assert result.returncode == EXIT_OPERATIONAL


def test_tui_new_job_uses_shared_validation(mock_env) -> None:
    inputs = job_ops.LaunchInputs(
        source_type="SqlTemplate",
        destination_type="Csv",
        launch_cwd=Path.cwd(),
        sql_path="missing.sql",
    )
    issues = job_ops.validation_issues(inputs, kerberos_ttl=3600, deep=False)
    assert issues and "Illegal combination" in issues[0]


def test_tui_screens_import_job_ops() -> None:
    from dispatch.screens import job_detail, new_job

    assert new_job.job_ops is job_ops
    assert job_detail.job_ops is job_ops


def test_main_job_dispatch_exit_codes(mock_env, monkeypatch) -> None:
    result = _invoke(["job", "list", "--json"], monkeypatch=monkeypatch)
    assert result.returncode == EXIT_OK
    assert json.loads(result.stdout) == {"jobs": []}


def test_job_ops_cancel_requires_yes(mock_env, tmp_path: Path) -> None:
    job_dir = _seed_job(tmp_path, state="Pending")
    with pytest.raises(job_ops.ConfirmationRequired):
        job_ops.cancel_job(job_dir.name, yes=False)


def test_advisor_ack_required(mock_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kerberos, "ticket_ttl_seconds_sync", lambda: 3600)
    launch_cwd = tmp_path / "cwd"
    launch_cwd.mkdir()
    sql_path = _write_sql(launch_cwd / "q.sql")
    inputs = job_ops.LaunchInputs(
        source_type="SqlFile",
        destination_type="Csv",
        launch_cwd=launch_cwd,
        sql_path=str(sql_path),
        table_name=job_ops.table_name_for_inputs(
            source_type="SqlFile",
            destination_type="Csv",
            table_suffix_or_full="adv",
        ),
    )
    fake_errors = (
        Finding(
            rule_id="R09",
            rule_name="cartesian-product",
            guideline="§8 joins",
            severity="error",
            detection="CROSS JOIN detected",
            remediation="Add an explicit join predicate",
        ),
    )

    def fake_prepare(inp, *, kerberos_ttl):
        plan = real_prepare(inp, kerberos_ttl=kerberos_ttl)
        return job_ops.LaunchPlan(
            inputs=plan.inputs,
            source=plan.source,
            destination=plan.destination,
            params=plan.params,
            sql_text=plan.sql_text,
            analysis=AnalysisResult(available=True, findings=fake_errors),
            resolved_sql_path=plan.resolved_sql_path,
        )

    real_prepare = job_ops.prepare_launch
    monkeypatch.setattr(job_ops, "prepare_launch", fake_prepare)
    plan = job_ops.prepare_launch(inputs, kerberos_ttl=3600)
    assert plan.analysis.errors()
    with pytest.raises(job_ops.AdvisorAcknowledgementRequired):
        job_ops.require_launch_confirmation(plan, yes=True, acknowledge_advisor=False)

    denied = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--sql",
            "q.sql",
            "--table",
            "adv_cli",
            "--yes",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert denied.returncode == EXIT_USAGE
    assert "acknowledge-advisor" in denied.stderr

    monkeypatch.setattr(process, "launch_runner_detached", lambda job_dir: 1)
    acked = _invoke(
        [
            "job",
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--sql",
            "q.sql",
            "--table",
            "adv_cli_ok",
            "--yes",
            "--acknowledge-advisor",
        ],
        monkeypatch=monkeypatch,
        cwd=launch_cwd,
    )
    assert acked.returncode == EXIT_OK

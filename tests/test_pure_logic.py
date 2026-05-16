"""Unit tests for pure-logic modules: kerberos, manifest, jobs.

None of these tests require subprocesses, filesystem I/O beyond tmp_path,
or the mock environment — they exercise deterministic functions directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dispatch import kerberos, manifest, jobs


# =============================================================================
# kerberos.parse_ttl_seconds
# =============================================================================

class TestParseTtlSeconds:
    """MIT Kerberos klist output parser."""

    def _klist(self, delta_seconds: int, now: datetime | None = None) -> str:
        base = now or datetime(2026, 5, 16, 10, 0, 0)
        expires = base + timedelta(seconds=delta_seconds)
        return (
            "Ticket cache: FILE:/tmp/krb5cc_mock\n"
            "Default principal: mock@EXAMPLE.COM\n"
            "\n"
            "Valid starting       Expires              Service principal\n"
            f"{base:%m/%d/%Y %H:%M:%S}  {expires:%m/%d/%Y %H:%M:%S}  krbtgt/EXAMPLE.COM@EXAMPLE.COM\n"
        )

    def test_valid_ticket_returns_ttl(self) -> None:
        now = datetime(2026, 5, 16, 10, 0, 0)
        output = self._klist(3600, now=now)
        result = kerberos.parse_ttl_seconds(output, now=now)
        assert result == 3600

    def test_expired_ticket_returns_zero(self) -> None:
        now = datetime(2026, 5, 16, 10, 0, 0)
        output = self._klist(-60, now=now)
        result = kerberos.parse_ttl_seconds(output, now=now)
        assert result == 0

    def test_ticket_about_to_expire_returns_small_positive(self) -> None:
        now = datetime(2026, 5, 16, 10, 0, 0)
        output = self._klist(1, now=now)
        result = kerberos.parse_ttl_seconds(output, now=now)
        assert result == 1

    def test_empty_output_returns_none(self) -> None:
        assert kerberos.parse_ttl_seconds("") is None

    def test_header_only_no_ticket_rows_returns_none(self) -> None:
        output = (
            "Ticket cache: FILE:/tmp/krb5cc_mock\n"
            "Default principal: mock@EXAMPLE.COM\n"
            "\n"
            "Valid starting       Expires              Service principal\n"
        )
        assert kerberos.parse_ttl_seconds(output) is None

    def test_malformed_date_columns_skipped(self) -> None:
        output = (
            "Valid starting       Expires              Service principal\n"
            "notadate  notadate  krbtgt/EXAMPLE.COM@EXAMPLE.COM\n"
        )
        assert kerberos.parse_ttl_seconds(output) is None

    def test_multiple_ticket_rows_uses_first_match(self) -> None:
        now = datetime(2026, 5, 16, 10, 0, 0)
        row1_exp = now + timedelta(seconds=7200)
        row2_exp = now + timedelta(seconds=100)
        output = (
            "Valid starting       Expires              Service principal\n"
            f"{now:%m/%d/%Y %H:%M:%S}  {row1_exp:%m/%d/%Y %H:%M:%S}  krbtgt/EXAMPLE.COM@EXAMPLE.COM\n"
            f"{now:%m/%d/%Y %H:%M:%S}  {row2_exp:%m/%d/%Y %H:%M:%S}  host/edge@EXAMPLE.COM\n"
        )
        result = kerberos.parse_ttl_seconds(output, now=now)
        assert result == 7200

    def test_real_mock_klist_output(self) -> None:
        """The output format the fake klist in mocks/bin produces."""
        now = datetime(2026, 5, 16, 10, 0, 0)
        expires = now + timedelta(seconds=28800)
        output = (
            "Ticket cache: FILE:/tmp/krb5cc_mock\n"
            "Default principal: mock@EXAMPLE.COM\n"
            "\n"
            "Valid starting       Expires              Service principal\n"
            f"{now:%m/%d/%Y %H:%M:%S}  {expires:%m/%d/%Y %H:%M:%S}  "
            "krbtgt/EXAMPLE.COM@EXAMPLE.COM\n"
        )
        result = kerberos.parse_ttl_seconds(output, now=now)
        assert result == 28800


# =============================================================================
# manifest.validate and LEGAL_CELLS
# =============================================================================

def _minimal_manifest(**overrides) -> dict:
    """Return a valid manifest dict, optionally overriding fields."""
    base: dict = {
        "schema_version": 1,
        "id": "20260516T100000Z_aabbcc",
        "tool": "dispatch",
        "user": "testuser",
        "source": {"type": "SqlFile"},
        "destination": {"type": "Csv"},
        "params": {},
        "orchestrator_calls": [{"script": "download_to_csv.py", "argv": ["python3", "download_to_csv.py"]}],
        "state": "Pending",
        "pid": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
    }
    base.update(overrides)
    return base


class TestManifestValidate:
    def test_valid_manifest_raises_nothing(self) -> None:
        manifest.validate(_minimal_manifest())

    def test_missing_required_key_raises(self) -> None:
        data = _minimal_manifest()
        del data["user"]
        with pytest.raises(ValueError, match="missing keys"):
            manifest.validate(data)

    def test_wrong_schema_version_raises(self) -> None:
        with pytest.raises(ValueError, match="schema_version"):
            manifest.validate(_minimal_manifest(schema_version=2))

    def test_wrong_tool_raises(self) -> None:
        with pytest.raises(ValueError, match="tool"):
            manifest.validate(_minimal_manifest(tool="other"))

    def test_empty_orchestrator_calls_raises(self) -> None:
        with pytest.raises(ValueError, match="orchestrator"):
            manifest.validate(_minimal_manifest(orchestrator_calls=[]))

    def test_invalid_state_raises(self) -> None:
        with pytest.raises(ValueError, match="state"):
            manifest.validate(_minimal_manifest(state="Queued"))

    @pytest.mark.parametrize("state", ["Pending", "Running", "Succeeded", "Failed", "Cancelled"])
    def test_all_valid_states_accepted(self, state: str) -> None:
        manifest.validate(_minimal_manifest(state=state))

    @pytest.mark.parametrize("source_type,dest_type", [
        ("SqlFile", "Table"),
        ("SqlFile", "Csv"),
        ("SqlFile", "Table+Csv"),
        ("SqlTemplate", "Table"),
        ("ExistingTable", "Csv"),
    ])
    def test_all_legal_cells_accepted(self, source_type: str, dest_type: str) -> None:
        data = _minimal_manifest(
            source={"type": source_type},
            destination={"type": dest_type},
        )
        manifest.validate(data)

    @pytest.mark.parametrize("source_type,dest_type", [
        ("SqlTemplate", "Csv"),
        ("SqlTemplate", "Table+Csv"),
        ("ExistingTable", "Table"),
        ("ExistingTable", "Table+Csv"),
        ("SqlFile", "Unknown"),
        ("Unknown", "Csv"),
    ])
    def test_illegal_cells_raise(self, source_type: str, dest_type: str) -> None:
        data = _minimal_manifest(
            source={"type": source_type},
            destination={"type": dest_type},
        )
        with pytest.raises(ValueError, match="illegal"):
            manifest.validate(data)


# =============================================================================
# manifest.build_orchestrator_calls — argv shape per legal cell
# =============================================================================

class TestBuildOrchestratorCalls:
    def _build(
        self,
        source_type: str,
        dest_type: str,
        tmp_path: Path,
        **dest_overrides,
    ) -> list[manifest.OrchestratorCall]:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        source: manifest.Source = {"type": source_type}  # type: ignore[assignment]
        dest: manifest.Destination = {  # type: ignore[assignment]
            "type": dest_type, "schema": "dw", "table_name": "t", **dest_overrides
        }
        params: dict = {"to_email": "x@y.com", "subject": "S", "start_date": "01/01/2026", "end_date": "02/01/2026"}
        return manifest.build_orchestrator_calls(job_dir, source, dest, params, tmp_path, "user1")

    def test_sqlfile_csv_single_download_call(self, tmp_path: Path) -> None:
        calls = self._build("SqlFile", "Csv", tmp_path)
        assert len(calls) == 1
        assert calls[0]["script"] == "download_to_csv.py"
        assert "--query-file" in calls[0]["argv"]

    def test_sqlfile_table_single_query_impala_call(self, tmp_path: Path) -> None:
        calls = self._build("SqlFile", "Table", tmp_path)
        assert len(calls) == 1
        assert calls[0]["script"] == "Query_Impala_Parametrized.py"

    def test_sqlfile_table_plus_csv_two_calls_in_order(self, tmp_path: Path) -> None:
        calls = self._build("SqlFile", "Table+Csv", tmp_path)
        assert len(calls) == 2
        assert calls[0]["script"] == "Query_Impala_Parametrized.py"
        assert calls[1]["script"] == "download_to_csv.py"
        # second call must use --table-name, not --query-file
        assert "--table-name" in calls[1]["argv"]
        assert "--query-file" not in calls[1]["argv"]

    def test_sqltemplate_table_monthly_processor(self, tmp_path: Path) -> None:
        calls = self._build("SqlTemplate", "Table", tmp_path)
        assert len(calls) == 1
        assert calls[0]["script"] == "monthly_query_processor.py"

    def test_existingtable_csv_download_by_table_name(self, tmp_path: Path) -> None:
        calls = self._build("ExistingTable", "Csv", tmp_path)
        assert len(calls) == 1
        assert calls[0]["script"] == "download_to_csv.py"
        assert "--table-name" in calls[0]["argv"]
        assert "--query-file" not in calls[0]["argv"]


# =============================================================================
# jobs.active_jobs, history_jobs, can_launch
# =============================================================================

def _write_manifest(jobs_dir: Path, state: str, finished_at: str | None = None) -> manifest.JobManifest:
    """Write a minimal manifest to disk and return it."""
    job_id = f"20260516T100000Z_{state[:6].lower().replace('+', 'p')}"
    # make each job_id unique by appending a counter based on dir count
    job_id = f"20260516T10{len(list(jobs_dir.glob('*'))):04d}00Z_aaaaaa"
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True)
    m: manifest.JobManifest = {
        "schema_version": 1,
        "id": job_id,
        "tool": "dispatch",
        "user": "testuser",
        "source": {"type": "SqlFile"},
        "destination": {"type": "Csv"},
        "params": {},
        "orchestrator_calls": [{"script": "download_to_csv.py", "argv": ["python3", "x.py"]}],
        "state": state,  # type: ignore[typeddict-item]
        "pid": None,
        "started_at": "2026-05-16T10:00:00Z",
        "finished_at": finished_at,
        "exit_code": None,
    }
    manifest.write(job_dir / "manifest.json", m)
    return m


class TestJobsListing:
    def test_no_jobs_dir_returns_empty(self, tmp_path: Path) -> None:
        assert jobs.active_jobs(root=tmp_path / "nonexistent") == []
        assert jobs.history_jobs(root=tmp_path / "nonexistent") == []

    def test_running_job_is_active(self, tmp_path: Path) -> None:
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        _write_manifest(jdir, "Running")
        active = jobs.active_jobs(root=jdir)
        assert len(active) == 1
        assert active[0]["state"] == "Running"

    def test_recent_succeeded_job_is_active(self, tmp_path: Path) -> None:
        from dispatch.manifest import now_utc
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        _write_manifest(jdir, "Succeeded", finished_at=now_utc())
        active = jobs.active_jobs(root=jdir)
        assert len(active) == 1

    def test_old_succeeded_job_is_history_not_active(self, tmp_path: Path) -> None:
        from datetime import timedelta
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_manifest(jdir, "Succeeded", finished_at=old_ts)
        assert jobs.active_jobs(root=jdir) == []
        history = jobs.history_jobs(root=jdir)
        assert len(history) == 1

    def test_just_inside_seven_day_boundary_is_active(self, tmp_path: Path) -> None:
        """Job finished just under 7 days ago is still in the active window."""
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        # 7 days minus 60 seconds — well inside ACTIVE_WINDOW even accounting
        # for the small delay between computing this timestamp and the jobs call.
        inside_ts = (datetime.now(timezone.utc) - timedelta(days=7) + timedelta(seconds=60)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _write_manifest(jdir, "Succeeded", finished_at=inside_ts)
        active = jobs.active_jobs(root=jdir)
        assert len(active) == 1

    def test_can_launch_true_when_no_running_jobs(self, tmp_path: Path) -> None:
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        assert jobs.can_launch(root=jdir) is True

    def test_can_launch_true_with_one_running(self, tmp_path: Path) -> None:
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        _write_manifest(jdir, "Running")
        assert jobs.can_launch(root=jdir) is True

    def test_can_launch_false_with_two_running(self, tmp_path: Path) -> None:
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        _write_manifest(jdir, "Running")
        _write_manifest(jdir, "Running")
        assert jobs.can_launch(root=jdir) is False

    def test_failed_and_cancelled_do_not_count_toward_cap(self, tmp_path: Path) -> None:
        from dispatch.manifest import now_utc
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        _write_manifest(jdir, "Failed", finished_at=now_utc())
        _write_manifest(jdir, "Cancelled", finished_at=now_utc())
        assert jobs.can_launch(root=jdir) is True

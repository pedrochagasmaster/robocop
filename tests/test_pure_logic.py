"""Unit tests for pure-logic modules: kerberos, manifest, jobs.

None of these tests require subprocesses, filesystem I/O beyond tmp_path,
or the mock environment — they exercise deterministic functions directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dispatch import config, impala, jobs, kerberos, manifest, sql

# =============================================================================
# sql identifier and CSV-path validation
# =============================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dispatch_smoke_1", None),
        ("_leading_underscore", None),
        ("144", "Table name must be a plain Impala identifier"),
        (
            "138936_das_curated_gco_cdl_daily_tinkoff",
            "Table name must be a plain Impala identifier",
        ),
        ("", "Table name must be a plain Impala identifier"),
        ("bad-name", "Table name must be a plain Impala identifier"),
        ("t;drop", "Table name must be a plain Impala identifier"),
        ("schema.table", "Table name must be a plain Impala identifier"),
        ("`quoted`", "Table name must be a plain Impala identifier"),
        ("has space", "Table name must be a plain Impala identifier"),
        ("a--b", "Table name must be a plain Impala identifier"),
    ],
)
def test_plain_impala_identifier_validation(value: str, expected: str | None) -> None:
    assert sql.validate_identifier(value, "Table name") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("aa_enc.dispatch_smoke_1", None),
        (
            "aa_enc.138936_das_curated_gco_cdl_daily_tinkoff",
            "Existing table must be schema.table using plain Impala identifiers",
        ),
        ("aa_enc.144", "Existing table must be schema.table using plain Impala identifiers"),
        (
            "schema.table.extra",
            "Existing table must be schema.table using plain Impala identifiers",
        ),
        ("schema.bad-name", "Existing table must be schema.table using plain Impala identifiers"),
        ("schema.t;drop", "Existing table must be schema.table using plain Impala identifiers"),
        ("", "Existing table must be schema.table using plain Impala identifiers"),
    ],
)
def test_full_impala_table_validation(value: str, expected: str | None) -> None:
    assert sql.validate_full_table(value, "Existing table") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dispatch_smoke_1", None),
        ("_leading_underscore", None),
        ("138936_das_curated_gco_cdl_daily_tinkoff", None),
        ("144", None),
        ("", "Table name must be a plain Impala identifier"),
        ("bad-name", "Table name must be a plain Impala identifier"),
        ("t;drop", "Table name must be a plain Impala identifier"),
        ("schema.table", "Table name must be a plain Impala identifier"),
        ("`quoted`", "Table name must be a plain Impala identifier"),
        ("has space", "Table name must be a plain Impala identifier"),
        ("a--b", "Table name must be a plain Impala identifier"),
    ],
)
def test_catalog_identifier_validation(value: str, expected: str | None) -> None:
    assert sql.validate_catalog_identifier(value, "Table name") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("aa_enc.dispatch_smoke_1", None),
        ("aa_enc.138936_das_curated_gco_cdl_daily_tinkoff", None),
        ("aa_enc.144", None),
        ("144.ok_table", None),
        (
            "schema.table.extra",
            "Table must be schema.table using plain Impala identifiers",
        ),
        ("schema.bad-name", "Table must be schema.table using plain Impala identifiers"),
        ("aa_enc.`144`", "Table must be schema.table using plain Impala identifiers"),
        ("aa_enc.table;drop", "Table must be schema.table using plain Impala identifiers"),
        ("aa_enc.table--x", "Table must be schema.table using plain Impala identifiers"),
        ("aa_enc.table name", "Table must be schema.table using plain Impala identifiers"),
        ("", "Table must be schema.table using plain Impala identifiers"),
    ],
)
def test_catalog_full_table_validation(value: str, expected: str | None) -> None:
    assert sql.validate_catalog_full_table(value, "Table") == expected


def test_classic_validation_still_rejects_digit_leading_for_jobs() -> None:
    """New Job / manifest / CSV shared validators must not accept digit-leading names."""
    assert sql.validate_identifier("123_enc", "Schema") == (
        "Schema must be a plain Impala identifier"
    )
    assert sql.validate_identifier("144", "Table name") == (
        "Table name must be a plain Impala identifier"
    )
    assert sql.validate_full_table("aa_enc.144", "Existing table") == (
        "Existing table must be schema.table using plain Impala identifiers"
    )
    assert sql.validate_full_table("123_enc.dispatch_smoke_1", "Destination table") == (
        "Destination table must be schema.table using plain Impala identifiers"
    )


def test_table_wrapper_does_not_accept_digit_leading_via_shared_validation() -> None:
    """Shared classic validation is the gate before table_wrapper interpolation."""
    assert sql.validate_identifier("123_enc", "Schema") is not None
    assert sql.validate_identifier("144", "Table name") is not None
    # table_wrapper itself does not validate; callers must use classic validators.
    # Digit-leading values must never pass those shared checks into this path.


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dispatch_smoke_1", "dispatch_smoke_1"),
        ("_leading_underscore", "_leading_underscore"),
        ("138936_das_curated_gco_cdl_daily_tinkoff", "`138936_das_curated_gco_cdl_daily_tinkoff`"),
        ("144", "`144`"),
    ],
)
def test_quote_identifier_backticks_digit_leading_names(value: str, expected: str) -> None:
    assert sql.quote_identifier(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("aa_enc.dispatch_smoke_1", "aa_enc.dispatch_smoke_1"),
        (
            "aa_enc.138936_das_curated_gco_cdl_daily_tinkoff",
            "aa_enc.`138936_das_curated_gco_cdl_daily_tinkoff`",
        ),
        ("aa_enc.144", "aa_enc.`144`"),
        ("144.ok_table", "`144`.ok_table"),
    ],
)
def test_format_full_table_sql_quotes_digit_leading_table(value: str, expected: str) -> None:
    assert sql.format_full_table_sql(value, "Table") == expected


def test_format_full_table_sql_rejects_unsafe_before_rendering() -> None:
    with pytest.raises(ValueError, match="schema.table using plain Impala identifiers"):
        sql.format_full_table_sql("aa_enc.bad-name", "Table")


def test_csv_path_is_confined_to_launch_cwd(tmp_path: Path) -> None:
    assert sql.safe_csv_path(tmp_path, "dispatch_smoke_1") == (
        tmp_path.resolve() / "dispatch_smoke_1.csv"
    )
    for unsafe_stem in ("", "../escape", r"..\escape", "bad/name", r"bad\name", "..", "144"):
        with pytest.raises(ValueError, match="CSV filename stem|plain Impala identifier"):
            sql.safe_csv_path(tmp_path, unsafe_stem)


def test_show_tables_rejects_unsafe_schema_before_query(monkeypatch) -> None:
    queries: list[str] = []

    async def fake_query(statement: str) -> str:
        queries.append(statement)
        return ""

    monkeypatch.setattr(impala, "query", fake_query)

    async def run() -> None:
        with pytest.raises(ValueError, match="Schema must be a plain Impala identifier"):
            await impala.show_tables("bad-schema")

    asyncio.run(run())
    assert queries == []


def test_show_tables_quotes_digit_leading_schema(monkeypatch) -> None:
    queries: list[str] = []

    async def fake_query(statement: str) -> str:
        queries.append(statement)
        return "name\nok_table\n"

    monkeypatch.setattr(impala, "query", fake_query)

    async def run() -> list[str]:
        return await impala.show_tables("144")

    assert asyncio.run(run()) == ["ok_table"]
    assert queries == ["SHOW TABLES IN `144` LIKE '*';"]


def test_show_tables_rejects_quoted_pattern_before_query(monkeypatch) -> None:
    queries: list[str] = []

    async def fake_query(statement: str) -> str:
        queries.append(statement)
        return ""

    monkeypatch.setattr(impala, "query", fake_query)

    async def run() -> None:
        with pytest.raises(ValueError, match="pattern must not contain a single quote"):
            await impala.show_tables("aa_enc", "x'; DROP TABLE y; --")

    asyncio.run(run())
    assert queries == []


@pytest.mark.parametrize("helper_name", ["describe_table", "drop_table", "table_stats"])
def test_full_table_helpers_reject_unsafe_name_before_query(monkeypatch, helper_name: str) -> None:
    queries: list[str] = []

    async def fake_query(statement: str) -> str:
        queries.append(statement)
        return ""

    monkeypatch.setattr(impala, "query", fake_query)

    async def run() -> None:
        helper = getattr(impala, helper_name)
        with pytest.raises(ValueError, match="Table must be schema.table"):
            await helper("schema.table.extra")

    asyncio.run(run())
    assert queries == []


@pytest.mark.parametrize(
    ("full_table", "expected_fragment"),
    [
        ("aa_enc.dispatch_smoke_1", "aa_enc.dispatch_smoke_1"),
        ("aa_enc.144", "aa_enc.`144`"),
        (
            "aa_enc.138936_das_curated_gco_cdl_daily_tinkoff",
            "aa_enc.`138936_das_curated_gco_cdl_daily_tinkoff`",
        ),
    ],
)
@pytest.mark.parametrize(
    ("helper_name", "sql_prefix"),
    [
        ("describe_table", "DESCRIBE "),
        ("drop_table", "DROP TABLE IF EXISTS "),
        ("table_stats", "SHOW TABLE STATS "),
    ],
)
def test_full_table_helpers_quote_digit_leading_names(
    monkeypatch, helper_name: str, sql_prefix: str, full_table: str, expected_fragment: str
) -> None:
    queries: list[str] = []

    async def fake_query(statement: str) -> str:
        queries.append(statement)
        if helper_name == "table_stats":
            return (
                "#Rows|#Files|Size|Bytes Cached|Format|Incremental stats\n"
                "-1|1|4.20MB|NOT CACHED|TEXT|false\n"
            )
        return "name|type|comment\nid|int|\n"

    monkeypatch.setattr(impala, "query", fake_query)

    async def run() -> None:
        helper = getattr(impala, helper_name)
        await helper(full_table)

    asyncio.run(run())
    assert queries == [f"{sql_prefix}{expected_fragment};"]


def _stats_ok_output() -> str:
    return (
        "#Rows|#Files|Size|Bytes Cached|Format|Incremental stats\n"
        "-1|1|4.20MB|NOT CACHED|TEXT|false\n"
    )


def test_iter_table_sizes_isolates_recoverable_failures(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """ValueError and ImpalaExecutionError must not abort later tables in the batch."""
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
    calls: list[str] = []

    async def fake_run_impala_shell(statement: str) -> str:
        calls.append(statement)
        if "`boom`" in statement or statement.endswith("boom;"):
            raise impala.ImpalaExecutionError("impala-shell exited 1: AnalysisException")
        return _stats_ok_output()

    monkeypatch.setattr(impala, "_run_impala_shell", fake_run_impala_shell)

    async def run() -> list[tuple[str, impala.TableStats]]:
        return [
            item
            async for item in impala.iter_table_sizes(
                "aa_enc",
                [
                    "ok_table",
                    "bad-name",
                    "144",
                    "boom",
                    "138936_das_curated_gco_cdl_daily_tinkoff",
                ],
            )
        ]

    with caplog.at_level(logging.WARNING, logger="dispatch.impala"):
        results = asyncio.run(run())

    by_name = {name: stats for name, stats in results}
    assert list(by_name) == [
        "ok_table",
        "bad-name",
        "144",
        "boom",
        "138936_das_curated_gco_cdl_daily_tinkoff",
    ]
    assert by_name["ok_table"].size_bytes == 4_404_019
    assert by_name["bad-name"].size_bytes is None
    assert by_name["bad-name"].size_display == "—"
    assert by_name["144"].size_bytes == 4_404_019
    assert by_name["boom"].size_bytes is None
    assert by_name["boom"].size_display == "—"
    assert by_name["138936_das_curated_gco_cdl_daily_tinkoff"].size_bytes == 4_404_019
    assert any("aa_enc.bad-name" in record.getMessage() for record in caplog.records)
    assert any("aa_enc.boom" in record.getMessage() for record in caplog.records)
    assert any("SHOW TABLE STATS aa_enc.ok_table;" in call for call in calls)
    assert any("SHOW TABLE STATS aa_enc.`144`;" in call for call in calls)
    assert any("SHOW TABLE STATS aa_enc.boom;" in call for call in calls)
    assert any(
        "SHOW TABLE STATS aa_enc.`138936_das_curated_gco_cdl_daily_tinkoff`;" in call
        for call in calls
    )
    assert not any("bad-name" in call for call in calls)


def test_iter_table_sizes_propagates_oserror(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))

    async def boom(_statement: str) -> str:
        raise FileNotFoundError("impala-shell")

    monkeypatch.setattr(impala, "_run_impala_shell", boom)

    async def run() -> None:
        async for _ in impala.iter_table_sizes("aa_enc", ["ok_table"]):
            pass

    with pytest.raises(FileNotFoundError, match="impala-shell"):
        asyncio.run(run())


def test_iter_table_sizes_propagates_typeerror(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))

    async def boom(_statement: str) -> str:
        raise TypeError("programmer bug")

    monkeypatch.setattr(impala, "_run_impala_shell", boom)

    async def run() -> None:
        async for _ in impala.iter_table_sizes("aa_enc", ["ok_table"]):
            pass

    with pytest.raises(TypeError, match="programmer bug"):
        asyncio.run(run())


def test_iter_table_sizes_propagates_cancellederror(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))

    async def boom(_statement: str) -> str:
        raise asyncio.CancelledError()

    monkeypatch.setattr(impala, "_run_impala_shell", boom)

    async def run() -> None:
        async for _ in impala.iter_table_sizes("aa_enc", ["ok_table"]):
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())


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


@pytest.mark.asyncio
async def test_has_ticket_returns_false_when_klist_times_out(monkeypatch) -> None:
    async def fake_run_exec(*_argv: str, timeout: float | None = None):
        raise TimeoutError("klist timed out")

    monkeypatch.setattr(kerberos.process, "run_exec", fake_run_exec)

    assert await kerberos.has_ticket() is False


@pytest.mark.asyncio
async def test_ticket_ttl_returns_none_when_klist_times_out(monkeypatch) -> None:
    async def fake_run_exec(*_argv: str, timeout: float | None = None):
        raise TimeoutError("klist timed out")

    monkeypatch.setattr(kerberos.process, "run_exec", fake_run_exec)

    assert await kerberos.ticket_ttl_seconds() is None


def test_sync_ttl_probe_resolves_klist_through_path(mock_env) -> None:
    """The CLI's Kerberos probe must find the PATH-injected mock klist.

    Windows ``CreateProcess`` searches System32 before PATH, so a bare
    ``klist`` would reach the OS tool and report no ticket even in dev mode.
    """
    ttl = kerberos.ticket_ttl_seconds_sync()

    assert ttl is not None
    assert ttl > kerberos.MIN_LAUNCH_TTL_SECONDS


def test_sync_ttl_probe_passes_a_resolved_command(mock_env, monkeypatch) -> None:
    seen: dict[str, list[str]] = {}

    def fake_run(argv, **_kwargs):
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(kerberos.subprocess, "run", fake_run)

    assert kerberos.ticket_ttl_seconds_sync() is None
    assert seen["argv"][0] != "klist"
    assert seen["argv"][-1].endswith("klist") or seen["argv"][-1].endswith("klist.exe")


def test_principal_for_eid_appends_realm(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_KRB5_REALM", "CORP.MASTERCARD.ORG")
    assert kerberos.principal_for_eid("jdoe") == "jdoe@CORP.MASTERCARD.ORG"
    assert kerberos.principal_for_eid("jdoe@OTHER.REALM") == "jdoe@OTHER.REALM"


@pytest.mark.asyncio
async def test_kinit_with_password_succeeds_with_mock_tools(mock_env, monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_MOCK_KLIST_TTL", "0")
    ok, message = await kerberos.kinit_with_password("testuser", "secret")
    assert ok is True
    assert message == ""
    assert await kerberos.has_ticket() is True


@pytest.mark.asyncio
async def test_kinit_with_password_reports_failure_without_logging_password(
    mock_env, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("DISPATCH_MOCK_KLIST_TTL", "0")
    monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "auth_error")
    password = "super-secret-password"
    ok, message = await kerberos.kinit_with_password("testuser", password)
    assert ok is False
    assert "Password incorrect" in message
    assert password not in caplog.text


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
        "orchestrator_calls": [
            {"script": "download_to_csv.py", "argv": ["python3", "download_to_csv.py"]}
        ],
        "state": "Pending",
        "pid": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
    }
    base.update(overrides)
    return base


def test_ensure_private_dir_tightens_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "private"
    existing.mkdir()
    chmod_calls: list[tuple[Path, int]] = []
    original_chmod = Path.chmod

    def record_chmod(self: Path, mode: int, *args, **kwargs) -> None:
        chmod_calls.append((self, mode))
        original_chmod(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", record_chmod)

    assert config.ensure_private_dir(existing) == existing
    assert (existing, 0o700) in chmod_calls


def test_write_config_tightens_dispatch_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
    chmod_calls: list[tuple[Path, int]] = []
    original_chmod = Path.chmod

    def record_chmod(self: Path, mode: int, *args, **kwargs) -> None:
        chmod_calls.append((self, mode))
        original_chmod(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", record_chmod)

    config.write_config({"form_defaults": {"email": "dispatch@example.com"}})

    dispatch_home = tmp_path / ".dispatch"
    assert (dispatch_home / "config.json").is_file()
    assert (dispatch_home, 0o700) in chmod_calls


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not reliable on Windows")
def test_create_job_makes_dispatch_job_tree_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))

    job_dir, _created = manifest.create_job(
        source={"type": "SqlFile"},
        destination={
            "type": "Csv",
            "schema": "aa_enc",
            "table_name": "dispatch_result",
            "csv_path": str(tmp_path / "dispatch_result.csv"),
        },
        params={"to_email": "", "subject": "Dispatch Job"},
        launch_cwd=tmp_path,
        sql_text="select 1\n",
        user="testuser",
    )

    dispatch_home = tmp_path / ".dispatch"
    jobs_root = dispatch_home / "jobs"
    assert stat.S_IMODE(dispatch_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(jobs_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(job_dir.stat().st_mode) == 0o700


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

    @pytest.mark.parametrize(
        "source_type,dest_type",
        [
            ("SqlFile", "Table"),
            ("SqlFile", "Csv"),
            ("SqlFile", "Table+Csv"),
            ("SqlTemplate", "Table"),
            ("ExistingTable", "Csv"),
        ],
    )
    def test_all_legal_cells_accepted(self, source_type: str, dest_type: str) -> None:
        data = _minimal_manifest(
            source={"type": source_type},
            destination={"type": dest_type},
        )
        manifest.validate(data)

    @pytest.mark.parametrize(
        "source_type,dest_type",
        [
            ("SqlTemplate", "Csv"),
            ("SqlTemplate", "Table+Csv"),
            ("ExistingTable", "Table"),
            ("ExistingTable", "Table+Csv"),
            ("SqlFile", "Unknown"),
            ("Unknown", "Csv"),
        ],
    )
    def test_illegal_cells_raise(self, source_type: str, dest_type: str) -> None:
        data = _minimal_manifest(
            source={"type": source_type},
            destination={"type": dest_type},
        )
        with pytest.raises(ValueError, match="illegal"):
            manifest.validate(data)

    def test_write_retries_transient_replace_permission_error(self, tmp_path, monkeypatch) -> None:
        original_replace = Path.replace
        calls = {"permission_errors": 0}

        def flaky_replace(self: Path, target: Path) -> Path:
            if self.name == "manifest.tmp" and calls["permission_errors"] == 0:
                calls["permission_errors"] += 1
                raise PermissionError("manifest temporarily locked")
            return original_replace(self, target)

        monkeypatch.setattr(Path, "replace", flaky_replace)
        path = tmp_path / "manifest.json"

        manifest.write(path, _minimal_manifest())

        assert calls["permission_errors"] == 1
        assert manifest.load(path)["state"] == "Pending"


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
            "type": dest_type,
            "schema": "dw",
            "table_name": "t",
            **dest_overrides,
        }
        params: dict = {
            "to_email": "x@y.com",
            "subject": "S",
            "start_date": "01/01/2026",
            "end_date": "02/01/2026",
        }
        return manifest.build_orchestrator_calls(job_dir, source, dest, params, tmp_path, "user1")

    def test_sqlfile_csv_single_download_call(self, tmp_path: Path) -> None:
        calls = self._build("SqlFile", "Csv", tmp_path)
        assert len(calls) == 1
        assert calls[0]["script"] == "download_to_csv.py"
        argv = calls[0]["argv"]
        assert "--query-file" in argv
        assert argv[argv.index("--to-email") + 1] == "x@y.com"
        assert argv[argv.index("--subject") + 1] == "S"

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
        argv = calls[1]["argv"]
        assert "--table-name" in argv
        assert "--query-file" not in argv
        assert argv[argv.index("--to-email") + 1] == "x@y.com"
        assert argv[argv.index("--subject") + 1] == "S"

    def test_sqltemplate_table_monthly_processor(self, tmp_path: Path) -> None:
        calls = self._build("SqlTemplate", "Table", tmp_path)
        assert len(calls) == 1
        assert calls[0]["script"] == "monthly_query_processor.py"

    def test_existingtable_csv_download_by_table_name(self, tmp_path: Path) -> None:
        calls = self._build("ExistingTable", "Csv", tmp_path)
        assert len(calls) == 1
        assert calls[0]["script"] == "download_to_csv.py"
        argv = calls[0]["argv"]
        assert "--table-name" in argv
        assert "--query-file" not in argv
        assert argv[argv.index("--to-email") + 1] == "x@y.com"
        assert argv[argv.index("--subject") + 1] == "S"

    def test_existingtable_rejects_unsafe_full_table_before_argv(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        source: manifest.Source = {
            "type": "ExistingTable",
            "table_name": "schema.bad-name",
        }
        destination: manifest.Destination = {
            "type": "Csv",
            "schema": "schema",
            "table_name": "bad-name",
        }

        with pytest.raises(ValueError, match="Existing table must be schema.table"):
            manifest.build_orchestrator_calls(
                job_dir,
                source,
                destination,
                {"to_email": "x@y.com", "subject": "S"},
                tmp_path,
                "user1",
            )

    @pytest.mark.parametrize(
        ("schema", "table", "message"),
        [
            ("bad-schema", "dispatch_smoke", "Schema must be a plain Impala identifier"),
            ("aa_enc", "bad-name", "Table name must be a plain Impala identifier"),
        ],
    )
    def test_table_destination_rejects_unsafe_identifiers_before_argv(
        self, tmp_path: Path, schema: str, table: str, message: str
    ) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        source: manifest.Source = {"type": "SqlFile"}
        destination: manifest.Destination = {
            "type": "Table",
            "schema": schema,
            "table_name": table,
        }

        with pytest.raises(ValueError, match=message):
            manifest.build_orchestrator_calls(
                job_dir,
                source,
                destination,
                {"to_email": "x@y.com", "subject": "S"},
                tmp_path,
                "user1",
            )

    def test_csv_fallback_rejects_unsafe_filename_stem(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        source: manifest.Source = {"type": "SqlFile"}
        destination: manifest.Destination = {
            "type": "Csv",
            "table_name": "../escape",
        }

        with pytest.raises(ValueError, match="safe CSV filename stem"):
            manifest.build_orchestrator_calls(
                job_dir,
                source,
                destination,
                {"to_email": "x@y.com", "subject": "S"},
                tmp_path,
                "user1",
            )

    def test_csv_fallback_stays_in_resolved_launch_cwd(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        nested = tmp_path / "nested"
        nested.mkdir()
        launch_cwd = nested / ".."
        destination: manifest.Destination = {
            "type": "Csv",
            "table_name": "dispatch_smoke_1",
        }

        calls = manifest.build_orchestrator_calls(
            job_dir,
            {"type": "SqlFile"},
            destination,
            {"to_email": "x@y.com", "subject": "S"},
            launch_cwd,
            "user1",
        )

        output_index = calls[0]["argv"].index("--output-file") + 1
        assert Path(calls[0]["argv"][output_index]) == (tmp_path.resolve() / "dispatch_smoke_1.csv")

    def test_explicit_csv_path_outside_launch_cwd_is_rejected(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        outside_csv = tmp_path.parent / "escape.csv"
        destination: manifest.Destination = {
            "type": "Csv",
            "table_name": "dispatch_smoke_1",
            "csv_path": str(outside_csv),
        }

        with pytest.raises(ValueError, match="CSV output path must stay within"):
            manifest.build_orchestrator_calls(
                job_dir,
                {"type": "SqlFile"},
                destination,
                {"to_email": "x@y.com", "subject": "S"},
                tmp_path,
                "user1",
            )


class TestEffectiveJobSql:
    """job.sql must carry the auto-generated CREATE TABLE wrapper for SqlFile
    table destinations, since Query_Impala_Parametrized.py runs it verbatim and
    would otherwise just run a bare SELECT and create nothing."""

    SELECT = "SELECT 1 AS smoke_test_value"

    def _sql(self, source_type: str, dest_type: str) -> str:
        source: manifest.Source = {"type": source_type}  # type: ignore[assignment]
        dest: manifest.Destination = {  # type: ignore[assignment]
            "type": dest_type,
            "schema": "aa_enc",
            "table_name": "dispatch_smoke_x",
        }
        return manifest._effective_job_sql(source, dest, self.SELECT, "user1")

    def test_sqlfile_table_is_wrapped_as_create_table(self) -> None:
        out = self._sql("SqlFile", "Table")
        assert "CREATE TABLE aa_enc.dispatch_smoke_x" in out
        assert "STORED AS PARQUET" in out
        assert "/das/aa/enc/user1/dispatch_smoke_x" in out
        assert out.rstrip().endswith(self.SELECT)

    def test_sqlfile_table_plus_csv_is_wrapped(self) -> None:
        out = self._sql("SqlFile", "Table+Csv")
        assert "CREATE TABLE aa_enc.dispatch_smoke_x" in out
        assert self.SELECT in out

    def test_sqlfile_csv_is_left_raw(self) -> None:
        # download_to_csv.py exports query results; it must receive the raw SELECT.
        assert self._sql("SqlFile", "Csv") == self.SELECT

    def test_sqltemplate_table_is_left_raw(self) -> None:
        # monthly_query_processor.py builds its own CREATE TABLE statements.
        assert self._sql("SqlTemplate", "Table") == self.SELECT

    def test_existingtable_csv_is_left_raw(self) -> None:
        assert self._sql("ExistingTable", "Csv") == self.SELECT

    def test_sqlfile_table_with_own_ddl_is_not_double_wrapped(self) -> None:
        ddl = "CREATE TABLE aa_enc.mine STORED AS PARQUET AS SELECT 1"
        source: manifest.Source = {"type": "SqlFile"}  # type: ignore[assignment]
        dest: manifest.Destination = {  # type: ignore[assignment]
            "type": "Table",
            "schema": "aa_enc",
            "table_name": "mine",
        }
        out = manifest._effective_job_sql(source, dest, ddl, "user1")
        assert out == ddl


class TestIsSelfContainedDdl:
    def test_bare_select_is_not_ddl(self) -> None:
        assert sql.is_self_contained_ddl("SELECT 1 AS x") is False

    def test_leading_with_cte_is_not_ddl(self) -> None:
        assert sql.is_self_contained_ddl("WITH t AS (SELECT 1) SELECT * FROM t") is False

    def test_create_table_is_ddl(self) -> None:
        assert sql.is_self_contained_ddl("CREATE TABLE foo AS SELECT 1") is True

    def test_insert_is_ddl(self) -> None:
        assert sql.is_self_contained_ddl("INSERT INTO foo SELECT 1") is True

    def test_comments_skipped_before_keyword(self) -> None:
        text = "-- header comment\n/* block */\n  create table foo as select 1"
        assert sql.is_self_contained_ddl(text) is True

    def test_comments_before_select_still_not_ddl(self) -> None:
        assert sql.is_self_contained_ddl("-- note\nSELECT 1") is False


# =============================================================================
# jobs.active_jobs, history_jobs, can_launch
# =============================================================================


def _write_manifest(
    jobs_dir: Path, state: str, finished_at: str | None = None
) -> manifest.JobManifest:
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
    def setup_method(self) -> None:
        jobs._manifest_cache.clear()

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
        inside_ts = (
            datetime.now(timezone.utc) - timedelta(days=7) + timedelta(seconds=60)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
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

    def test_can_launch_false_with_two_pending_jobs(self, tmp_path: Path) -> None:
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        _write_manifest(jdir, "Pending")
        _write_manifest(jdir, "Pending")
        assert jobs.can_launch(root=jdir) is False

    def test_can_launch_false_when_three_slot_jobs_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
        jdir = tmp_path / ".dispatch" / "jobs"
        jdir.mkdir(parents=True)
        _write_manifest(jdir, "Running")
        _write_manifest(jdir, "Pending")
        _write_manifest(jdir, "Running")
        sql_file = tmp_path / "query.sql"
        sql_file.write_text("select 1\n", encoding="utf-8")

        assert jobs.count_launch_slot_jobs(root=jdir) == 3
        assert jobs.can_launch(root=jdir) is False
        with pytest.raises(jobs.LaunchSlotUnavailable):
            jobs.create_job_if_slot_available(
                source={"type": "SqlFile", "sql_path_at_launch": str(sql_file)},
                destination={
                    "type": "Csv",
                    "schema": "aa_enc",
                    "table_name": "dispatch_result",
                    "csv_path": str(tmp_path / "dispatch_result.csv"),
                },
                params={"to_email": "", "subject": "Dispatch Job"},
                launch_cwd=tmp_path,
                sql_text="select 1\n",
                user="testuser",
            )

    def test_failed_and_cancelled_do_not_count_toward_cap(self, tmp_path: Path) -> None:
        from dispatch.manifest import now_utc

        jdir = tmp_path / "jobs"
        jdir.mkdir()
        _write_manifest(jdir, "Failed", finished_at=now_utc())
        _write_manifest(jdir, "Cancelled", finished_at=now_utc())
        assert jobs.can_launch(root=jdir) is True

    def test_launch_slot_short_circuit_stops_after_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = [Path(f"/jobs/job-{index}/manifest.json") for index in range(10)]
        calls: list[Path] = []

        def fake_reconcile(path: Path) -> manifest.JobManifest:
            calls.append(path)
            return _minimal_manifest(id=path.parent.name, state="Running")

        monkeypatch.setattr(jobs, "_manifest_paths", lambda root=None: paths)
        monkeypatch.setattr(jobs, "reconcile_manifest", fake_reconcile)

        launch_slots = jobs.launch_slot_jobs(root=Path("/jobs"))

        assert len(launch_slots) == jobs.RUNNING_CAP
        assert calls == paths[: jobs.RUNNING_CAP]

    def test_active_jobs_cache_skips_unchanged_old_terminal_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        old_ts = (datetime.now(timezone.utc) - jobs.ACTIVE_WINDOW - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        for _ in range(5):
            _write_manifest(jdir, "Succeeded", finished_at=old_ts)

        assert jobs.active_jobs(root=jdir) == []

        loads = 0
        original_load = manifest.load

        def counting_load(path: Path) -> manifest.JobManifest:
            nonlocal loads
            loads += 1
            return original_load(path)

        monkeypatch.setattr(manifest, "load", counting_load)

        assert jobs.active_jobs(root=jdir) == []
        assert loads == 0

    def test_history_jobs_cache_skips_unchanged_old_terminal_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        old_ts = (datetime.now(timezone.utc) - jobs.ACTIVE_WINDOW - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        for _ in range(5):
            _write_manifest(jdir, "Succeeded", finished_at=old_ts)

        history = jobs.history_jobs(root=jdir)
        assert len(history) == 5

        calls: list[Path] = []
        original_reconcile = jobs.reconcile_manifest

        def counting_reconcile(path: Path) -> manifest.JobManifest | None:
            calls.append(path)
            return original_reconcile(path)

        monkeypatch.setattr(jobs, "reconcile_manifest", counting_reconcile)

        history = jobs.history_jobs(root=jdir)
        assert len(history) == 5
        assert calls == []

    def test_history_jobs_rechecks_manifest_after_old_terminal_mtime_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jdir = tmp_path / "jobs"
        jdir.mkdir()
        old_ts = (datetime.now(timezone.utc) - jobs.ACTIVE_WINDOW - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        item = _write_manifest(jdir, "Succeeded", finished_at=old_ts)
        history = jobs.history_jobs(root=jdir)
        assert [entry["id"] for entry in history] == [item["id"]]

        manifest_path = next(jdir.glob("*/manifest.json"))
        current_mtime = manifest_path.stat().st_mtime
        os.utime(manifest_path, (current_mtime + 5, current_mtime + 5))

        calls: list[Path] = []
        original_reconcile = jobs.reconcile_manifest

        def counting_reconcile(path: Path) -> manifest.JobManifest | None:
            calls.append(path)
            return original_reconcile(path)

        monkeypatch.setattr(jobs, "reconcile_manifest", counting_reconcile)

        history = jobs.history_jobs(root=jdir)
        assert [entry["id"] for entry in history] == [item["id"]]
        assert calls == [manifest_path]

    def test_create_job_if_slot_available_serializes_one_remaining_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
        jdir = tmp_path / ".dispatch" / "jobs"
        jdir.mkdir(parents=True)
        _write_manifest(jdir, "Running")
        sql_file = tmp_path / "query.sql"
        sql_file.write_text("select 1\n", encoding="utf-8")

        def create_one() -> Path | jobs.LaunchSlotUnavailable:
            try:
                job_dir, _created = jobs.create_job_if_slot_available(
                    source={"type": "SqlFile", "sql_path_at_launch": str(sql_file)},
                    destination={
                        "type": "Csv",
                        "schema": "aa_enc",
                        "table_name": "dispatch_result",
                        "csv_path": str(tmp_path / "dispatch_result.csv"),
                    },
                    params={"to_email": "", "subject": "Dispatch Job"},
                    launch_cwd=tmp_path,
                    sql_text="select 1\n",
                    user="testuser",
                )
                return job_dir
            except jobs.LaunchSlotUnavailable as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: create_one(), range(2)))

        successes = [result for result in results if isinstance(result, Path)]
        failures = [result for result in results if isinstance(result, jobs.LaunchSlotUnavailable)]
        assert len(successes) == 1
        assert len(failures) == 1
        manifests = [
            path
            for path in jdir.glob("*/manifest.json")
            if manifest.load(path)["state"] in {"Pending", "Running"}
        ]
        assert len(manifests) == jobs.RUNNING_CAP

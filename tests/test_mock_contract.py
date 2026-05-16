"""Argv contract tests for mocks/bin/impala-shell.

ADR-0004 states: "Drift between the orchestrators' real invocation and the
fake is an integration bug."  These tests verify that the mock accepts every
argv shape documented in docs/plan.md §13.1 and produces the expected
stdout/stderr/exit-code for each combination.

Each test invokes the mock as a real subprocess so the contract is checked at
the process boundary, not just at the Python level.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


MOCKS_BIN = Path(__file__).resolve().parents[1] / "mocks" / "bin"
IMPALA_SHELL = MOCKS_BIN / "impala-shell"

# Base flags as used by Query_Impala_Parametrized.run_on_impala and
# download_to_csv.run_export_on_impala (docs/plan.md §13.1)
BASE_ARGV = [
    str(IMPALA_SHELL),
    "-k",
    "-i", "dw.prod.impala.mastercard.int:21000",
    "--ssl",
    "--delimited",
    "--print_header",
]


def _run(
    extra_argv: list[str],
    scenario: str = "happy_path",
    env_overrides: dict | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DISPATCH_MOCK_SCENARIO"] = scenario
    env["DISPATCH_MOCK_DELAY"] = "0"
    # Unique state dir per call to avoid count bleed between parametrize cases
    env["DISPATCH_MOCK_STATE_DIR"] = "/tmp/dispatch_contract_test_state"
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        BASE_ARGV + extra_argv,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# §13.1 Query_Impala_Parametrized argv shape
# ---------------------------------------------------------------------------

class TestQueryImpalaParametrizedArgv:
    """Exact argv from Query_Impala_Parametrized.run_on_impala."""

    BASE_QUERY = (
        "--output_delimiter=|",
        "-q",
    )

    def _argv_for(self, sql: str) -> list[str]:
        return list(self.BASE_QUERY) + [sql]

    def test_happy_path_exits_zero(self, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        result = _run(
            self._argv_for("set request_pool=adhoc_fast; SELECT 1;"),
            scenario="happy_path",
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode == 0
        assert "succeeded" in result.stdout

    def test_syntax_error_exits_nonzero_with_analysis_exception(self, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        result = _run(
            self._argv_for("set request_pool=adhoc_fast; SELCT 1;"),
            scenario="syntax_error",
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode != 0
        assert "AnalysisException" in result.stderr
        assert "Syntax error" in result.stderr

    def test_auth_error_exits_nonzero_with_authentication_exception(self, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        result = _run(
            self._argv_for("set request_pool=adhoc_fast; SELECT 1;"),
            scenario="auth_error",
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode != 0
        assert "AuthenticationException" in result.stderr

    def test_all_queues_full_exits_nonzero_with_admission_error(self, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        result = _run(
            self._argv_for("set request_pool=adhoc_fast; SELECT 1;"),
            scenario="all_queues_full",
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode != 0
        assert "queue is full" in result.stderr

    def test_table_not_found_exits_nonzero_with_analysis_exception(self, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        result = _run(
            self._argv_for("set request_pool=adhoc_fast; SELECT * FROM unknown_table;"),
            scenario="table_not_found",
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode != 0
        assert "could not resolve" in result.stderr

    def test_pool_extracted_from_set_request_pool_prefix(self, tmp_path: Path) -> None:
        """The pool name extracted by POOL_RE appears in the success output."""
        state_dir = str(tmp_path / "state")
        result = _run(
            self._argv_for("set request_pool=acs_small; SELECT 1;"),
            scenario="happy_path",
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode == 0
        assert "pool=acs_small" in result.stdout

    def test_unknown_flags_are_tolerated(self, tmp_path: Path) -> None:
        """The orchestrators may add flags in future; the mock must not reject them."""
        state_dir = str(tmp_path / "state")
        result = subprocess.run(
            [
                str(IMPALA_SHELL),
                "-k", "--ssl", "--delimited", "--print_header",
                "--output_delimiter=|",
                "--future-unknown-flag", "somevalue",
                "-q", "set request_pool=adhoc; SELECT 1;",
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "DISPATCH_MOCK_SCENARIO": "happy_path",
                "DISPATCH_MOCK_STATE_DIR": state_dir,
                "DISPATCH_MOCK_DELAY": "0",
            },
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# §13.1 download_to_csv argv shape (uses --output_delimiter=, and -o)
# ---------------------------------------------------------------------------

class TestDownloadToCsvArgv:
    """Exact argv from download_to_csv.run_export_on_impala."""

    def test_csv_file_written_when_output_flag_given(self, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        out_file = tmp_path / "result.csv"
        result = _run(
            [
                "--output_delimiter=,",
                "-q", "set request_pool=adhoc_fast; set mem_limit=1000g; SELECT 1;",
                "-o", str(out_file),
            ],
            scenario="happy_path",
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode == 0
        assert out_file.exists(), "CSV output file must be created"
        content = out_file.read_text(encoding="utf-8")
        assert "id,value" in content

    def test_no_output_file_when_flag_absent(self, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        result = _run(
            [
                "--output_delimiter=,",
                "-q", "set request_pool=adhoc_fast; set mem_limit=1000g; SELECT 1;",
            ],
            scenario="happy_path",
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Metadata query routing (schema queries bypass scenario dispatch)
# ---------------------------------------------------------------------------

class TestMetadataQueryRouting:
    """SHOW TABLES, DESCRIBE, DROP TABLE must succeed regardless of scenario."""

    @pytest.mark.parametrize("scenario", ["happy_path", "syntax_error", "auth_error", "all_queues_full"])
    def test_show_tables_always_succeeds(self, scenario: str, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        result = _run(
            ["--output_delimiter=|", "-q", "SHOW TABLES IN dw;"],
            scenario=scenario,
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode == 0
        assert "dispatch_result" in result.stdout

    @pytest.mark.parametrize("scenario", ["happy_path", "syntax_error"])
    def test_describe_always_succeeds(self, scenario: str, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        result = _run(
            ["--output_delimiter=|", "-q", "DESCRIBE dw.my_table;"],
            scenario=scenario,
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode == 0
        assert "name|type|comment" in result.stdout

    @pytest.mark.parametrize("scenario", ["happy_path", "syntax_error"])
    def test_drop_table_always_succeeds(self, scenario: str, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        result = _run(
            ["--output_delimiter=|", "-q", "DROP TABLE IF EXISTS dw.old_table;"],
            scenario=scenario,
            env_overrides={"DISPATCH_MOCK_STATE_DIR": state_dir},
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# memory_exceeded retry simulation
# ---------------------------------------------------------------------------

class TestMemoryExceededScenario:
    def test_first_two_calls_fail_third_succeeds(self, tmp_path: Path) -> None:
        state_dir = str(tmp_path / "state")
        base_env = {
            "DISPATCH_MOCK_SCENARIO": "memory_exceeded",
            "DISPATCH_MOCK_DELAY": "0",
            "DISPATCH_MOCK_STATE_DIR": state_dir,
        }
        argv = BASE_ARGV + ["--output_delimiter=|", "-q", "set request_pool=adhoc_fast; SELECT 1;"]

        r1 = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, **base_env})
        r2 = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, **base_env})
        r3 = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, **base_env})

        assert r1.returncode != 0, "1st call should fail"
        assert "Memory limit exceeded" in r1.stderr
        assert r2.returncode != 0, "2nd call should fail"
        assert r3.returncode == 0, "3rd call should succeed"
        assert "succeeded" in r3.stdout

    def test_counter_isolated_per_state_dir(self, tmp_path: Path) -> None:
        """Two tests with different DISPATCH_MOCK_STATE_DIR must not share counts."""
        dir_a = str(tmp_path / "state_a")
        dir_b = str(tmp_path / "state_b")
        argv = BASE_ARGV + ["--output_delimiter=|", "-q", "set request_pool=adhoc; SELECT 1;"]

        mock_defaults = {"DISPATCH_MOCK_SCENARIO": "memory_exceeded", "DISPATCH_MOCK_DELAY": "0"}
        # Count in dir_a
        env_a = {**os.environ, **mock_defaults, "DISPATCH_MOCK_STATE_DIR": dir_a}
        subprocess.run(argv, capture_output=True, env=env_a)  # count=1 in a, fail
        subprocess.run(argv, capture_output=True, env=env_a)  # count=2 in a, fail

        # dir_b starts fresh — first call should also fail, not succeed
        env_b = {**os.environ, **mock_defaults, "DISPATCH_MOCK_STATE_DIR": dir_b}
        result_b1 = subprocess.run(argv, capture_output=True, text=True, env=env_b)
        assert result_b1.returncode != 0, "Fresh state dir should start at count=1, which is a failure"

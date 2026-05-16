"""Shared pytest fixtures for the Dispatch test suite."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


WORKSPACE = Path(__file__).resolve().parents[1]
MOCKS_BIN = WORKSPACE / "mocks" / "bin"
SCR_DIR = WORKSPACE / "scr"


@pytest.fixture()
def mock_env(tmp_path, monkeypatch):
    """Inject the full mock environment into the current test process.

    Sets PATH, DISPATCH_* env vars, MAILHOST, and per-test state/data dirs so
    every test is fully isolated from disk state left by prior runs.

    The ``DISPATCH_MOCK_STATE_DIR`` is a fresh directory under ``tmp_path``
    so call-count state (used by the ``memory_exceeded`` scenario) never
    leaks between tests.

    Returns a dict with ``state_dir`` and ``data_root`` ``Path`` objects for
    tests that need to inspect or seed those directories directly.
    """
    state_dir = tmp_path / "mock_state"
    data_root = tmp_path / "data"
    state_dir.mkdir()
    data_root.mkdir()

    monkeypatch.setenv("PATH", f"{MOCKS_BIN}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    monkeypatch.setenv("DISPATCH_MOCK_STATE_DIR", str(state_dir))
    monkeypatch.setenv("DISPATCH_DATA_ROOT", str(data_root))
    monkeypatch.setenv("DISPATCH_SCR_DIR", str(SCR_DIR))
    monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "happy_path")
    monkeypatch.setenv("DISPATCH_MOCK_DELAY", "0")
    # Port 9 (Discard) is virtually never bound; connection is refused instantly,
    # so orchestrator email attempts fail fast without hanging the test.
    monkeypatch.setenv("MAILHOST", "127.0.0.1:9")

    return {"state_dir": state_dir, "data_root": data_root}


@pytest.fixture()
def mock_env_with_config(mock_env):
    """Like ``mock_env`` but also writes a minimal dispatch config.json.

    Used by TUI tests that call ``config.read_config()`` on startup.
    """
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    dispatch_home = data_root / ".dispatch"
    dispatch_home.mkdir(parents=True, exist_ok=True)
    config_path = dispatch_home / "config.json"
    config_path.write_text(
        json.dumps({"to_email": "test@example.com"}),
        encoding="utf-8",
    )
    return mock_env

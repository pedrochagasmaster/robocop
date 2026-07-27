"""Configuration and data-root helpers for Dispatch."""

import getpass
import json
import os
from pathlib import Path
from typing import Any


def current_user() -> str:
    return os.environ.get("USER") or getpass.getuser()


def data_root(user: str | None = None) -> Path:
    override = os.environ.get("DISPATCH_DATA_ROOT")
    if override:
        return Path(override)
    return Path("/ads_storage") / (user or current_user())


def dispatch_home(user: str | None = None, *, root: Path | None = None) -> Path:
    return (root if root is not None else data_root(user)) / ".dispatch"


def ensure_private_dir(path: Path) -> Path:
    """Create a per-user Dispatch directory and enforce owner-only access."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def jobs_dir(user: str | None = None) -> Path:
    return dispatch_home(user) / "jobs"


def notebook_dir(user: str | None = None, *, root: Path | None = None) -> Path:
    """Notebook workspace: Dispatch-owned Inline SQL and Results (ADR-0010)."""
    return dispatch_home(user, root=root) / "notebook"


def config_path(user: str | None = None) -> Path:
    return dispatch_home(user) / "config.json"


def read_config(user: str | None = None) -> dict[str, Any]:
    path = config_path(user)
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_config(config: dict[str, Any], user: str | None = None) -> None:
    path = config_path(user)
    ensure_private_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_form_defaults(user: str | None = None) -> dict[str, str]:
    """Read last-used form defaults from config, returning empty dict on failure."""
    try:
        cfg = read_config(user)
        defaults = cfg.get("form_defaults", {})
        return defaults if isinstance(defaults, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def save_form_defaults(values: dict[str, str], user: str | None = None) -> None:
    """Merge form defaults into the existing config, creating if needed."""
    try:
        cfg = read_config(user)
    except (OSError, ValueError, json.JSONDecodeError):
        cfg = {}
    cfg["form_defaults"] = values
    write_config(cfg, user)

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


def impala_monitor_ca_bundle() -> str | None:
    """Optional CA bundle path for validating coordinator TLS certificates.

    ``None`` (the default) means "use the system trust store" via
    ``ssl.create_default_context()`` with no ``cafile`` override. Set via
    ``DISPATCH_IMPALA_MONITOR_CA_BUNDLE`` for environments with a private CA.
    This never disables verification; it only points verification at a
    specific bundle.
    """
    override = os.environ.get("DISPATCH_IMPALA_MONITOR_CA_BUNDLE", "").strip()
    return override or None


def impala_monitor_allow_http() -> bool:
    """Dev/mock-only opt-in to allow plaintext ``http://`` coordinator URLs.

    Defaults to ``False`` (HTTPS-only, verified). Set
    ``DISPATCH_IMPALA_MONITOR_ALLOW_HTTP=1`` only for local mock servers; this
    must never be enabled in a production deployment.
    """
    raw = os.environ.get("DISPATCH_IMPALA_MONITOR_ALLOW_HTTP", "").strip().lower()
    return raw not in ("", "0", "false", "off", "no")


def impala_monitor_seed_url() -> str | None:
    """Optional validated coordinator seed for operator-triggered recovery."""
    override = os.environ.get("DISPATCH_IMPALA_MONITOR_SEED_URL", "").strip()
    return override or None

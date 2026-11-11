"""Where expenser keeps its files (overridable through environment variables for tests/portable use)."""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["state_dir", "output_dir", "user_flows_dir", "bundled_flows_dir"]


def state_dir() -> Path:
    """Private working data: browser profiles, ledger, screenshots, user flows (``~/.expenser``)."""
    env = os.environ.get("EXPENSER_HOME")
    return Path(env).expanduser() if env else Path.home() / ".expenser"


def output_dir() -> Path:
    """Where the Excel files you work with are saved (``~/Expenser``)."""
    env = os.environ.get("EXPENSER_OUTPUT_DIR")
    return Path(env).expanduser() if env else Path.home() / "Expenser"


def user_flows_dir() -> Path:
    return state_dir() / "flows"


def bundled_flows_dir() -> Path:
    """Flows shipped inside the package (works for wheels and source checkouts)."""
    from importlib.resources import files

    return Path(str(files("expenser") / "flows"))

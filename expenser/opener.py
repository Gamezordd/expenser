"""Open a file with the operating system's default application."""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from rich.console import Console

__all__ = ["open_in_default_app"]


def open_in_default_app(path: Path, console: Console | None = None) -> bool:
    """Open a file with the OS default app. Never raises; returns success."""
    console = console or Console()
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        elif system == "Darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001 - opening is best effort
        console.print(f"[yellow]Could not open automatically. Open this file manually:[/] {path}")
        return False

"""Browser lifecycle for flows.

Three ways to get a Playwright ``(context, page)``:

* ``bundled``: Playwright's own Chromium via ``launch_persistent_context``.
* ``chrome``: the user's real Google Chrome, started as a *normal* process (no automation
  flags, so Google sign-in accepts it) with a remote-debugging port and a dedicated
  user-data-dir; Playwright attaches over CDP.
* ``cdp_url``: attach to a browser that is already running; it is never closed by us.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from expenser.automation.flow import Flow

__all__ = ["find_chrome", "open_session", "default_profile_dir", "chrome_profile_dir", "BrowserError"]

_LINUX_NAMES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")
_FIXED_PATHS = (
    "/opt/google/chrome/chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
_INSTALL_HINT = (
    "Google Chrome was not found. On Debian/Ubuntu install it with:\n"
    "  wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && "
    "sudo apt install ./google-chrome-stable_current_amd64.deb\n"
    "Or point to an existing binary with --chrome-path, `settings.chrome_path` in the flow, "
    "or the EXPENSER_CHROME environment variable."
)


class BrowserError(RuntimeError):
    """The browser could not be started or attached."""


def _windows_paths() -> list[str]:
    out = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            out.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    return out


def find_chrome(explicit: str | None = None) -> str:
    """Locate a Chrome/Chromium executable. Order: argument, $EXPENSER_CHROME, PATH, standard paths."""
    for candidate in (explicit, os.environ.get("EXPENSER_CHROME")):
        if candidate:
            resolved = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
            if not resolved:
                raise BrowserError(f"Chrome executable not found at '{candidate}'.")
            return resolved
    for name in _LINUX_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in (*_FIXED_PATHS, *_windows_paths()):
        if Path(path).is_file():
            return path
    raise BrowserError(_INSTALL_HINT)


def default_profile_dir(flow: Flow) -> Path:
    from expenser.paths import state_dir

    return state_dir() / "profiles" / flow.name


def chrome_profile_dir(flow: Flow) -> Path:
    # Must be a non-default user-data-dir: Chrome 136+ refuses remote debugging on the default one.
    from expenser.paths import state_dir

    return state_dir() / "chrome-profiles" / flow.name


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_debugger(proc: subprocess.Popen, port: int, timeout: float = 20.0) -> None:
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise BrowserError(
                "Chrome exited immediately. The profile directory is probably already open in "
                "another Chrome window (or a previous run is still alive); close that window "
                "and try again."
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                json.load(resp)
                return
        except Exception:
            time.sleep(0.2)
    raise BrowserError(f"Chrome did not open its debugging port {port} within {timeout:.0f}s.")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@contextmanager
def open_session(
    flow: Flow,
    *,
    headless: bool = False,
    profile_dir: Path | None = None,
    browser: str | None = None,
    cdp_url: str | None = None,
    chrome_path: str | None = None,
) -> Iterator[tuple[BrowserContext, Page]]:
    """Yield ``(context, page)`` for the flow's browser mode (see module docstring)."""
    settings = flow.settings
    with sync_playwright() as pw:
        if cdp_url:
            br = pw.chromium.connect_over_cdp(cdp_url, slow_mo=settings.slow_mo_ms)
            context = br.contexts[0] if br.contexts else br.new_context()
            context.set_default_timeout(settings.default_timeout_ms)
            page = context.new_page()
            try:
                yield context, page
            finally:
                # Only close what we opened; the user's browser stays alive.
                try:
                    if not page.is_closed():
                        page.close()
                except Exception:
                    pass
            return

        mode = browser or settings.browser
        if mode == "chrome":
            exe = find_chrome(chrome_path or settings.chrome_path)
            profile = profile_dir or chrome_profile_dir(flow)
            profile.mkdir(parents=True, exist_ok=True)
            port = _free_port()
            args = [
                exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check", "about:blank",
            ]
            if headless:
                args.append("--headless=new")
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                _wait_for_debugger(proc, port)
                br = pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}", slow_mo=settings.slow_mo_ms
                )
                try:
                    context = br.contexts[0] if br.contexts else br.new_context()
                    context.set_default_timeout(settings.default_timeout_ms)
                    page = context.pages[0] if context.pages else context.new_page()
                    yield context, page
                finally:
                    try:
                        br.close()
                    except Exception:
                        pass
            finally:
                _stop(proc)
            return

        profile = profile_dir or default_profile_dir(flow)
        profile.mkdir(parents=True, exist_ok=True)
        context = pw.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            slow_mo=settings.slow_mo_ms,
            **({} if headless else {"no_viewport": True}),
        )
        try:
            context.set_default_timeout(settings.default_timeout_ms)
            page = context.pages[0] if context.pages else context.new_page()
            yield context, page
        finally:
            context.close()

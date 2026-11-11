"""Browser lifecycle tests. Playwright's bundled full Chromium stands in for Chrome."""
from __future__ import annotations

import glob
import os
import socket
import subprocess
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

from expenser.automation import browser as br
from expenser.automation.browser import BrowserError, find_chrome, open_session
from expenser.automation.flow import load_flow
from expenser.automation.runner import open_session as runner_open_session, push_transactions
from expenser.models import Transaction

FIXTURES = Path(__file__).parent / "fixtures"
FLOW = FIXTURES / "mock_flow.yaml"
APP_URL = (FIXTURES / "mock_expense_app.html").as_uri()


def _chromium() -> str:
    hits = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    if not hits:
        pytest.skip("bundled Chromium not installed")
    return hits[-1]


def _txs():
    return [
        Transaction(name="Groceries", amount=Decimal("12.50"), participants=["Alice", "Bob"], paid_by="Alice", date=None),
        Transaction(name="Taxi", amount=Decimal("8.00"), participants=["Bob", "Carol"], paid_by="Bob", date=None),
    ]


def _push(tmp_path, **kw):
    return push_transactions(
        FLOW, _txs(), all_participants=["Alice", "Bob", "Carol"], variables={"app_url": APP_URL},
        headless=True, state_dir=tmp_path / "state", profile_dir=tmp_path / "profile", **kw,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zombies count as gone
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


def test_runner_reexports_open_session():
    assert runner_open_session is open_session


def test_find_chrome_precedence(monkeypatch, tmp_path):
    fake = tmp_path / "mychrome"
    fake.write_text("")
    other = tmp_path / "envchrome"
    other.write_text("")
    monkeypatch.setattr(br.shutil, "which", lambda n: f"/usr/bin/{n}" if n == "chromium" else None)
    monkeypatch.setenv("EXPENSER_CHROME", str(other))
    assert find_chrome(str(fake)) == str(fake)
    assert find_chrome(None) == str(other)
    monkeypatch.delenv("EXPENSER_CHROME")
    assert find_chrome(None) == "/usr/bin/chromium"
    with pytest.raises(BrowserError, match="not found"):
        find_chrome(str(tmp_path / "missing"))


def test_find_chrome_none_gives_install_hint(monkeypatch):
    monkeypatch.delenv("EXPENSER_CHROME", raising=False)
    monkeypatch.setattr(br.shutil, "which", lambda n: None)
    monkeypatch.setattr(br, "_FIXED_PATHS", ())
    monkeypatch.setattr(br, "_windows_paths", lambda: [])
    with pytest.raises(BrowserError, match="apt install"):
        find_chrome(None)


def test_chrome_mode_end_to_end(tmp_path, monkeypatch):
    pids = []
    real = subprocess.Popen

    def spy(*a, **k):
        p = real(*a, **k)
        if a and "--remote-debugging-port" in " ".join(map(str, a[0])):
            pids.append(p.pid)
        return p

    monkeypatch.setattr(br.subprocess, "Popen", spy)
    results = _push(tmp_path, browser="chrome", chrome_path=_chromium())
    assert [r.status for r in results] == ["pushed", "pushed"]
    assert len(pids) == 1
    time.sleep(0.3)
    assert not _alive(pids[0])


def test_chrome_mode_via_flow_settings(tmp_path):
    flow = load_flow(FLOW)
    assert flow.settings.browser == "bundled"
    flow.settings.browser = "chrome"
    flow.settings.chrome_path = _chromium()
    with open_session(flow, headless=True, profile_dir=tmp_path / "p") as (ctx, page):
        page.goto(APP_URL)
        assert page.locator("[data-testid=group-header]").count() == 1


def test_cdp_url_attach_leaves_browser_alive(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [_chromium(), f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'ext'}",
         "--no-first-run", "--headless=new", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        results = _push(tmp_path, cdp_url=f"http://127.0.0.1:{port}")
        assert [r.status for r in results] == ["pushed", "pushed"]
        assert proc.poll() is None
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
    finally:
        proc.kill()
        proc.wait()


def test_early_exit_error(tmp_path):
    fake = tmp_path / "fakechrome.sh"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    flow = load_flow(FLOW)
    with pytest.raises(BrowserError, match="already open"):
        with open_session(flow, headless=True, profile_dir=tmp_path / "p", browser="chrome",
                          chrome_path=str(fake)):
            pass

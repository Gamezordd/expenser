import io

import pytest
from rich.console import Console

from expenser import auth, doctor
from expenser.automation import browser


@pytest.fixture
def env(monkeypatch, tmp_path):
    # doctor checks the same dirs the app uses (expenser.paths), which honour these overrides
    monkeypatch.setenv("EXPENSER_HOME", str(tmp_path / ".expenser"))
    monkeypatch.setenv("EXPENSER_OUTPUT_DIR", str(tmp_path / "Expenser"))
    monkeypatch.setattr(browser, "find_chrome", lambda explicit=None: "/fake/chrome")
    monkeypatch.setattr(auth, "describe_credentials", lambda: auth.CredentialStatus("api_key", "key", True))
    monkeypatch.setattr(auth, "find_ant", lambda: None)
    monkeypatch.setattr(doctor, "_check_flows", lambda: (doctor.OK, "flows", ""))
    return tmp_path


def _run():
    buf = io.StringIO()
    code = doctor.run_doctor(Console(file=buf, width=120, force_terminal=False))
    return code, buf.getvalue()


def test_all_ok_optional_missing_is_zero(env, monkeypatch):
    monkeypatch.setattr(auth, "describe_credentials", lambda: auth.CredentialStatus("none", "x", False))
    code, out = _run()
    assert code == 0
    assert "ant CLI" in out and "optional" in out
    assert (env / ".expenser").is_dir() and (env / "Expenser").is_dir()


def test_missing_chrome_fails(env, monkeypatch):
    def boom(explicit=None):
        raise browser.BrowserError("nope")

    monkeypatch.setattr(browser, "find_chrome", boom)
    code, out = _run()
    assert code == 1
    assert "google.com/chrome" in out


def test_unwritable_data_dir_fails(env, monkeypatch):
    (env / ".expenser").write_text("a file, not a dir")
    code, _ = _run()
    assert code == 1


def test_missing_flows_folder_does_not_crash(env, monkeypatch):
    import importlib.resources

    monkeypatch.undo()  # restore real _check_flows; re-patch the rest
    monkeypatch.setenv("EXPENSER_HOME", str(env / ".expenser"))
    monkeypatch.setenv("EXPENSER_OUTPUT_DIR", str(env / "Expenser"))
    monkeypatch.setattr(browser, "find_chrome", lambda explicit=None: "/fake/chrome")
    monkeypatch.setattr(auth, "find_ant", lambda: None)
    monkeypatch.setattr(auth, "describe_credentials", lambda: auth.CredentialStatus("none", "x", False))

    class Missing:
        def __truediv__(self, other):
            return self

        def is_dir(self):
            return False

    monkeypatch.setattr(importlib.resources, "files", lambda pkg: Missing())
    status, msg, _ = doctor._check_flows()
    assert status == doctor.WARN
    code, _ = _run()
    assert code == 0

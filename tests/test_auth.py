import json
import os
import sys
import time
import types
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

import expenser.auth as auth
import expenser.cli as cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("ANTHROPIC_"):
            monkeypatch.delenv(k)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(cfg))
    return cfg


def write_profile(cfg: Path, name="default"):
    (cfg / "configs").mkdir(exist_ok=True)
    (cfg / "credentials").mkdir(exist_ok=True)
    (cfg / "configs" / f"{name}.json").write_text(
        json.dumps({"authentication": {"type": "user_oauth"}, "organization_id": "org"})
    )
    cred = cfg / "credentials" / f"{name}.json"
    cred.write_text(json.dumps({
        "type": "oauth_token", "access_token": "tok-fake", "expires_at": int(time.time()) + 10**7,
    }))
    cred.chmod(0o600)
    (cfg / "configs" / f"{name}.json").chmod(0o600)


def test_none():
    s = auth.describe_credentials()
    assert s.source == "none" and not s.ok


def test_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    s = auth.describe_credentials()
    assert s.source == "api_key" and s.ok and not s.warnings
    assert "sk-ant" not in s.detail


def test_empty_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    s = auth.describe_credentials()
    assert s.source == "none" and not s.ok
    assert s.warnings and "empty" in s.warnings[0]


def test_empty_api_key_does_not_block_login(clean_env, monkeypatch):
    # The SDK skips an empty key and uses the login profile, so we must too.
    write_profile(clean_env)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    s = auth.describe_credentials()
    assert s.source == "profile" and s.ok
    assert any("empty" in w for w in s.warnings)


def test_auth_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-secret")
    s = auth.describe_credentials()
    assert s.source == "auth_token" and s.ok and "tok-secret" not in s.detail


def test_profile(clean_env):
    write_profile(clean_env)
    s = auth.describe_credentials()
    assert s.source == "profile" and s.ok and "default" in s.detail


def test_api_key_shadows_profile(clean_env, monkeypatch):
    write_profile(clean_env)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    s = auth.describe_credentials()
    assert s.source == "api_key" and any("overrides" in w for w in s.warnings)


def test_login_without_ant(monkeypatch):
    monkeypatch.setattr(auth, "find_ant", lambda: None)
    r = runner.invoke(cli.app, ["auth", "login"])
    assert r.exit_code == 1 and "brew install anthropics/tap/ant" in r.output


def test_login_with_ant(monkeypatch, clean_env):
    seen = []
    monkeypatch.setattr(auth, "find_ant", lambda: "/usr/bin/ant")
    monkeypatch.setattr(auth, "run_ant", lambda args: seen.append(args) or 0)
    r = runner.invoke(cli.app, ["auth", "login", "--profile", "work", "--no-browser"])
    assert r.exit_code == 0, r.output
    assert seen == [["auth", "login", "--profile", "work", "--no-browser"]]


def test_login_warns_about_api_key(monkeypatch):
    monkeypatch.setattr(auth, "find_ant", lambda: "/usr/bin/ant")
    monkeypatch.setattr(auth, "run_ant", lambda args: 0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    r = runner.invoke(cli.app, ["auth", "login"])
    assert "OVERRIDE" in r.output


def test_logout_args(monkeypatch):
    seen = []
    monkeypatch.setattr(auth, "find_ant", lambda: "/usr/bin/ant")
    monkeypatch.setattr(auth, "run_ant", lambda args: seen.append(args) or 0)
    r = runner.invoke(cli.app, ["auth", "logout", "--all"])
    assert r.exit_code == 0 and seen == [["auth", "logout", "--all"]]


def test_status_exit_codes(clean_env, monkeypatch):
    assert runner.invoke(cli.app, ["auth", "status"]).exit_code == 1
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert runner.invoke(cli.app, ["auth", "status"]).exit_code == 0


def test_extract_preflight_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.txt").write_text("Alice\nBob\n")
    Image.new("RGB", (20, 20)).save(tmp_path / "s.png")
    called = []
    monkeypatch.setattr(cli, "extract_transactions", lambda *a, **k: called.append(1))
    r = runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review"])
    assert r.exit_code == 1
    assert "expenser auth login" in r.output.replace("\n", " ")
    assert not called

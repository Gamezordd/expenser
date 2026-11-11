"""Credential discovery and the `ant` CLI login helper (never prints secrets)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

ANT_INSTALL_HELP = """The Anthropic CLI (`ant`) is needed to sign in, but it was not found on your PATH.

Install it:
  macOS:  brew install anthropics/tap/ant
  Linux:  download a release tarball from https://github.com/anthropics/anthropic-cli/releases
          and put the `ant` binary in a directory on your PATH
  Go:     go install github.com/anthropics/anthropic-cli/v2/cmd/ant@latest

Then run `expenser auth login` again. Alternatively set ANTHROPIC_API_KEY in .env."""


@dataclass
class CredentialStatus:
    source: str  # api_key | auth_token | profile | workload_identity | none | error
    detail: str
    ok: bool
    warnings: list[str] = field(default_factory=list)


def config_dir() -> Path:
    env = os.environ.get("ANTHROPIC_CONFIG_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Anthropic"
    return Path.home() / ".config" / "anthropic"


def active_profile_on_disk() -> str | None:
    """Name of the active login profile if its config file exists (else None)."""
    cdir = config_dir()
    name = os.environ.get("ANTHROPIC_PROFILE") or None
    if name is None:
        try:
            name = (cdir / "active_config").read_text(encoding="utf-8").strip() or None
        except OSError:
            name = None
    name = name or "default"
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    return name if (cdir / "configs" / f"{name}.json").is_file() else None


def describe_credentials() -> CredentialStatus:
    warnings: list[str] = []
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    # The SDK ignores empty values (truthiness check), so an empty var is harmless
    # for auth — but it's almost always a leftover line, so point it out.
    for name, val in (("ANTHROPIC_API_KEY", api_key), ("ANTHROPIC_AUTH_TOKEN", token)):
        if val is not None and val == "":
            warnings.append(
                f"{name} is set but empty and will be ignored; consider removing the line "
                f"from .env (or `unset {name}`)."
            )

    if api_key:
        profile = active_profile_on_disk()
        if profile:
            warnings.append(
                f"ANTHROPIC_API_KEY overrides your login profile '{profile}'. To use the login, "
                "remove or comment out ANTHROPIC_API_KEY (in .env or your shell)."
            )
        return CredentialStatus("api_key", "ANTHROPIC_API_KEY from environment/.env", True, warnings)

    try:
        from anthropic.lib.credentials import default_credentials

        result = default_credentials()
    except anthropic.AnthropicError as e:
        if not os.environ.get("ANTHROPIC_PROFILE") and active_profile_on_disk() is None:
            # ANTHROPIC_CONFIG_DIR / active_config pointer set, but no such profile: just not signed in.
            return CredentialStatus("none", "No credentials found", False, warnings)
        return CredentialStatus("error", str(e), False, warnings)
    except Exception as e:  # noqa: BLE001 - never let diagnostics crash the CLI
        return CredentialStatus("error", f"{type(e).__name__}: {e}", False, warnings)

    if result is None:
        return CredentialStatus("none", "No credentials found", False, warnings)

    provider = result.provider
    if hasattr(provider, "profile") and hasattr(provider, "config_path"):
        detail = f"Login profile '{provider.profile}' (config dir: {config_dir()})"
        return CredentialStatus("profile", detail, True, warnings)
    if type(provider).__name__ == "StaticToken":
        return CredentialStatus("auth_token", "ANTHROPIC_AUTH_TOKEN from environment/.env", True, warnings)
    return CredentialStatus("workload_identity", "Workload identity federation (environment)", True, warnings)


def find_ant() -> str | None:
    return shutil.which("ant")


def run_ant(args: list[str]) -> int:
    """Run `ant <args>` with inherited stdio; return its exit code."""
    exe = find_ant()
    if exe is None:
        return 127
    return subprocess.call([exe, *args])

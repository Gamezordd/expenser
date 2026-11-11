<#
.SYNOPSIS
  expenser installer for Windows (idempotent).
.EXAMPLE
  irm https://raw.githubusercontent.com/Gamezordd/expenser/main/scripts/install.ps1 | iex
  .\install.ps1 -Source .\expenser-0.2.0-py3-none-any.whl
.NOTES
  Default source: the latest code on GitHub (downloaded as a zip, so git is not needed). Re-run to update.
#>
param([Parameter(Position = 0)][string]$Source = "expenser @ https://github.com/Gamezordd/expenser/archive/refs/heads/main.zip")

$ErrorActionPreference = 'Stop'

# (a) uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> Installing uv (https://astral.sh/uv) ..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
}
$env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    # No `exit`: when run via `irm ... | iex` it would close the user's PowerShell window.
    Write-Host "uv was installed but is not on PATH. Open a new terminal and re-run this script."
    return
}

# (b) install
Write-Host "==> Installing expenser from $Source ..."
uv tool install --force --reinstall-package expenser --python 3.12 $Source
if ($LASTEXITCODE -ne 0) { throw "Installing expenser failed (uv exit code $LASTEXITCODE)." }

# (c) PATH for new terminals
uv tool update-shell

# (d) Chrome
$candidates = @()
foreach ($v in 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA') {
    $base = [Environment]::GetEnvironmentVariable($v)
    if ($base) { $candidates += (Join-Path $base 'Google\Chrome\Application\chrome.exe') }
}
if ($env:EXPENSER_CHROME) { $candidates += $env:EXPENSER_CHROME }
$found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($found) {
    Write-Host "==> Google Chrome: found."
} else {
    Write-Host "==> Google Chrome was NOT found. expenser needs it: https://www.google.com/chrome/"
}

# (e) next steps
Write-Host ""
Write-Host "Done. Open a NEW terminal (Windows Terminal recommended) and run:  expenser"
Write-Host "Troubleshooting:                                                    expenser doctor"

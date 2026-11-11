# Remove expenser (uv itself is left installed).
$ErrorActionPreference = 'Stop'
$env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Write-Host "uv not found; nothing to uninstall."; exit 1 }
uv tool uninstall expenser
Write-Host "expenser removed. Your data was left in place:"
Write-Host "  ~\.expenser  (login profile and state; delete it to sign out everywhere)"
Write-Host "  ~\Expenser   (your Excel files)"

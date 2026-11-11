#!/usr/bin/env sh
# Remove expenser (uv itself is left installed).
set -eu
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
command -v uv >/dev/null 2>&1 || { echo "uv not found; nothing to uninstall." >&2; exit 1; }
uv tool uninstall expenser
echo "expenser removed. Your data was left in place:"
echo "  ~/.expenser  (login profile and state; delete it to sign out everywhere)"
echo "  ~/Expenser   (your Excel files)"

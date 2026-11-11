#!/usr/bin/env sh
# expenser installer for macOS / Linux. Idempotent.
# Usage:  curl -LsSf https://raw.githubusercontent.com/Gamezordd/expenser/main/scripts/install.sh | sh
#         sh install.sh [SOURCE]
#   SOURCE (optional): a .whl file, a git URL, or an archive URL. Default: the latest code on GitHub
#   (downloaded as a zip, so git is not needed). Re-run to update.
set -eu

SOURCE="${1:-expenser @ https://github.com/Gamezordd/expenser/archive/refs/heads/main.zip}"

# (a) uv
if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv (https://astral.sh/uv) ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# Make uv visible in this session (installer puts it in ~/.local/bin or ~/.cargo/bin).
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export PATH
if ! command -v uv >/dev/null 2>&1; then
  echo "uv was installed but is not on PATH. Open a new terminal and re-run this script." >&2
  exit 1
fi

# (b) install
echo "==> Installing expenser from $SOURCE ..."
uv tool install --force --reinstall-package expenser --python 3.12 "$SOURCE"

# (c) PATH for new terminals
uv tool update-shell || true

# (d) Chrome
chrome_found=0
for n in google-chrome-stable google-chrome chromium chromium-browser; do
  command -v "$n" >/dev/null 2>&1 && chrome_found=1
done
for p in /opt/google/chrome/chrome "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
  [ -f "$p" ] && chrome_found=1
done
if [ -n "${EXPENSER_CHROME:-}" ] && [ -f "$EXPENSER_CHROME" ]; then chrome_found=1; fi
if [ "$chrome_found" = 1 ]; then
  echo "==> Google Chrome: found."
else
  echo "==> Google Chrome was NOT found. expenser needs it: https://www.google.com/chrome/"
fi

# (e) next steps
echo
echo "Done. Open a NEW terminal and run:  expenser"
echo "Troubleshooting:                    expenser doctor"

#!/usr/bin/env sh
# Author-side: build sdist + wheel into dist/ and print the wheel to share.
set -eu
cd "$(dirname "$0")/.."
rm -rf dist
if command -v uv >/dev/null 2>&1; then
  uv build --out-dir dist
elif [ -x .venv/bin/python ] && .venv/bin/python -c "import build" >/dev/null 2>&1; then
  .venv/bin/python -m build --outdir dist
else
  PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
  if "$PY" -c "import build" >/dev/null 2>&1; then
    "$PY" -m build --outdir dist
  else
    "$PY" -m pip wheel . -w dist --no-deps
  fi
fi
echo
echo "Built:"
ls -1 dist
WHL=$(ls -1 dist/*.whl | head -n1)
echo
echo "Share this wheel: $WHL"
echo "Friends install with:  sh install.sh ./$(basename "$WHL")"

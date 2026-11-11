#!/bin/zsh
# Create the development virtualenv (Apple Silicon, Python 3.11+).
set -e
cd "$(dirname "$0")/.."
PY=${PYTHON:-/opt/homebrew/bin/python3.12}
[[ "$(uname -m)" == "arm64" ]] || { echo "Apple Silicon (arm64) required"; exit 1; }
$PY -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-dev.txt
scripts/build_native.sh          # Metal compositor (libVCSNative.dylib); the app falls back to CPU without it
scripts/build_dev_app.sh         # dev app bundle (camera permission)
.venv/bin/python scripts/inspect_env.py

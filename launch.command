#!/bin/bash
#
# TranslateAI launcher — double-click this file in Finder to start the app.
#
# On first run it creates a local virtual environment and installs the Python
# dependencies; after that it just launches the app.

set -euo pipefail

# Always work from the folder this script lives in (Finder launches from $HOME).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

alert() { osascript -e "display alert \"TranslateAI\" message \"$1\"" >/dev/null 2>&1; }

# Pick a Python 3 interpreter.
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  alert "Python 3 is required. Install it from https://www.python.org/downloads/ or run: brew install python"
  echo "Python 3 not found." >&2
  exit 1
fi

# Create the venv + install deps on first run.
if [ ! -x ".venv/bin/python" ]; then
  echo "First run — setting up the environment…"
  "$PY" -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip >/dev/null
  if ! ./.venv/bin/pip install -r requirements.txt; then
    alert "Could not install dependencies. PyAudio needs PortAudio — run: brew install portaudio   then try again. See README.md."
    echo "Dependency install failed (PortAudio missing?)." >&2
    exit 1
  fi
fi

exec ./.venv/bin/python app.py

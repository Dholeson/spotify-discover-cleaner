#!/usr/bin/env bash
# Discover Cleaner launcher for macOS and Linux.
#   ./run.sh                  start the app (http://127.0.0.1:8888); the first start shows the Setup screen
#   ./run.sh --demo           offline demo: simulated Spotify and the author's listening history
#   ./run.sh --demo --reset   start the demo over
#   ./run.sh --port 9000      serve on another port
# Creates .venv on the first run and installs requirements.txt whenever it changes. No .env needed.
set -euo pipefail
cd "$(dirname "$0")"

# --- Python 3.10 or newer ---
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Discover Cleaner needs Python 3.10 or newer (python3 --version)." >&2
  echo "Install it from https://www.python.org/downloads/ or your package manager, then run this again." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating a virtual environment in .venv ..."
  if ! "$PY" -m venv .venv; then
    echo "Could not create the virtual environment. On Debian/Ubuntu: sudo apt install python3-venv" >&2
    exit 1
  fi
fi

# --- install requirements on the first run and whenever requirements.txt changes ---
if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing dependencies ..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi

exec .venv/bin/python app.py "$@"

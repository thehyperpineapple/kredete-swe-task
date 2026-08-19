#!/usr/bin/env bash
# One-command startup. Creates a venv on first run, installs deps, serves the app.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "==> Creating virtualenv in $VENV"
  "$PY" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [ ! -f "$VENV/.deps-installed" ] || [ requirements.txt -nt "$VENV/.deps-installed" ]; then
  echo "==> Installing dependencies"
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  touch "$VENV/.deps-installed"
fi

echo "==> Serving on http://127.0.0.1:8000"
exec uvicorn app:app --reload --host 127.0.0.1 --port "${PORT:-8000}"

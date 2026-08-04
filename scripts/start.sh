#!/usr/bin/env bash
# Start API for local (.venv) or Render (system python3).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "No Python found. Create .venv locally or use a Python runtime on Render." >&2
  exit 1
fi

exec "$PY" -m uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8787}"

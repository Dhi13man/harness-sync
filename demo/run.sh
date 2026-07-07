#!/usr/bin/env bash
# Launch the meta-agent-sync live demo on http://127.0.0.1:8765  (PORT=NNNN to override)
set -euo pipefail
cd "$(dirname "$0")"

# The sync engine needs Python 3.11+ (stdlib tomllib). Under an older interpreter
# it re-execs itself and would hijack the server, so pick a modern one explicitly.
PY=""
for c in "${DEMO_PYTHON:-}" .venv/bin/python python3.14 python3.13 python3.12 python3.11 python3; do
  if [ -z "$c" ] || ! command -v "$c" >/dev/null 2>&1; then
    continue
  fi
  if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Need Python 3.11+ (the engine requires stdlib tomllib)." >&2
  echo "Install it, or point DEMO_PYTHON at one:  DEMO_PYTHON=/usr/bin/python3.14 ./run.sh" >&2
  exit 1
fi

if ! "$PY" -c 'import flask' 2>/dev/null; then
  echo "Flask is missing for $($PY --version 2>&1). Install it:" >&2
  echo "    python3.14 -m venv .venv" >&2
  echo "    .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

echo "meta-agent-sync demo · $($PY --version 2>&1) → http://127.0.0.1:${PORT:-8765}"
exec "$PY" server.py

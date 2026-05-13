#!/usr/bin/env bash
# run.sh  –  Launch FinancialIntelligence with the project's virtual environment
#
# Usage:
#   ./run.sh                        # interactive menu
#   ./run.sh import path/to/file.pdf
#   ./run.sh balance 2025-01
#   ./run.sh transactions 2025-01
#   ./run.sh classify
#   ./run.sh memory
#   ./run.sh summary
#   ./run.sh setup
#
# First-time setup (creates .venv and installs dependencies):
#   ./run.sh --install

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PYTHON="$VENV/bin/python3"

if [[ "${1:-}" == "--install" ]]; then
  echo "Creating virtual environment…"
  python3 -m venv "$VENV"
  echo "Installing dependencies (bypassing corporate proxy for PyPI)…"
  NO_PROXY="pypi.org,files.pythonhosted.org" \
  HTTPS_PROXY="" HTTP_PROXY="" \
    "$VENV/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
  echo "✓ Installation complete. Run: ./run.sh"
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Virtual environment not found. Run: ./run.sh --install"
  exit 1
fi

cd "$SCRIPT_DIR"
exec "$PYTHON" main.py "$@"

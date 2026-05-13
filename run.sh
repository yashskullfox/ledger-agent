#!/usr/bin/env bash
# run.sh  –  Launch FinancialIntelligence with the project's virtual environment
#
# Usage:
#   ./run.sh                                  # interactive menu
#   ./run.sh scan   [FOLDER] [FLAGS]          # 📅 coverage wizard + import (R-45)
#   ./run.sh onboard [FOLDER] [FLAGS]         # alias for scan
#   ./run.sh import path/to/file.pdf          # import a single statement PDF
#   ./run.sh balance 2025-01                  # view balance sheet
#   ./run.sh tax 2025-01                      # view tax obligation estimate
#   ./run.sh context 2025-01                  # export AI context JSON
#   ./run.sh transactions 2025-01
#   ./run.sh classify
#   ./run.sh memory
#   ./run.sh summary
#   ./run.sh setup
#   ./run.sh mcp                              # start MCP stdio server (for Claude Desktop / Cursor)
#
# scan / onboard flags:
#   --force                          Re-import already-imported statements
#   --no-prompt                      CI mode: emit JSON coverage matrix, exit 0/2
#   --window 2025-01:2025-12         Override rolling 12-month window
#   --report                         Show balance sheet + tax after import
#
# Folder resolution order (scan / onboard):
#   1. Positional argument           ./run.sh scan ~/Documents/statements/
#   2. FI_STATEMENTS_DIR env var     FI_STATEMENTS_DIR=~/Statements ./run.sh scan
#   3. Last-used path (cached)
#   4. Default data/statements/
#
# CI / scripted usage:
#   ./run.sh scan FOLDER --no-prompt < /dev/null
#   → prints JSON coverage matrix; exit 0 if complete, 2 if gaps remain
#
# AI Modes (set FI_AI_BACKEND env var):
#   FI_AI_BACKEND=local   ./run.sh            # rule-based, no API key (default)
#   FI_AI_BACKEND=openai  ./run.sh            # OpenAI GPT-4o-mini
#   FI_AI_BACKEND=gemini  ./run.sh            # Google Gemini 1.5 Flash
#
# First-time setup (creates .venv and installs dependencies):
#   ./run.sh --install
#
# With AI backends:
#   ./run.sh --install-ai      # install + OpenAI + Gemini extras

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PYTHON="$VENV/bin/python3"

if [[ "${1:-}" == "--install" || "${1:-}" == "--install-ai" ]]; then
  echo "Creating virtual environment…"
  python3 -m venv "$VENV"
  echo "Installing dependencies (bypassing corporate proxy for PyPI)…"
  NO_PROXY="pypi.org,files.pythonhosted.org" \
  HTTPS_PROXY="" HTTP_PROXY="" \
    "$VENV/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
  if [[ "${1:-}" == "--install-ai" ]]; then
    echo "Installing AI backend extras (openai, google-generativeai, tenacity)…"
    NO_PROXY="pypi.org,files.pythonhosted.org" \
    HTTPS_PROXY="" HTTP_PROXY="" \
      "$VENV/bin/pip" install openai google-generativeai tenacity python-dotenv -q
    echo "✓ AI extras installed."
  fi
  echo "✓ Installation complete. Run: ./run.sh"
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Virtual environment not found. Run: ./run.sh --install"
  exit 1
fi

cd "$SCRIPT_DIR"
exec "$PYTHON" main.py "$@"

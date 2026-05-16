#!/usr/bin/env bash
# run.sh  –  ledger-agent bootstrap launcher (ARCH-05)
# =====================================================
# Creates / activates a Python venv, installs dependencies, and runs
# the ledger CLI or any legacy main.py command.
#
# Usage (new ledger commands):
#   ./run.sh scan [FOLDER] [--no-prompt] [--allow-partial]
#   ./run.sh balance [YEAR]
#   ./run.sh tax     [YEAR]
#   ./run.sh form1065 [YEAR]
#   ./run.sh k1 [YEAR] [--partner partner_1|partner_2]
#   ./run.sh reconcile [YEAR]
#
# Usage (legacy main.py pass-through):
#   ./run.sh mcp                      # start MCP stdio server
#   ./run.sh context 2025-01          # export AI context JSON
#   ./run.sh classify                 # batch-classify transactions
#
# Flags:
#   --no-prompt     CI mode (no interactive prompts)
#   --allow-partial Skip R-45 completeness gate
#   --partner NAME  Partner for k1 command (partner_1|partner_2)
#
# Environment:
#   FI_STATEMENTS_DIR   Override default statements folder
#   FI_DB_PATH          Override SQLite database path
#   FI_AI_BACKEND       local | openai | gemini  (default: local)
#   FI_OPENAI_API_KEY   Required if backend=openai
#   FI_GEMINI_API_KEY   Required if backend=gemini

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON_MIN="3.10"

# ── 1. Locate Python ──────────────────────────────────────────────────────────
_find_python() {
    for py in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$py" &>/dev/null; then
            ver=$("$py" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null)
            if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
                echo "$py"; return
            fi
        fi
    done
    echo >&2 "ERROR: Python >= $PYTHON_MIN not found. Install it and retry."
    exit 1
}
PYTHON=$(_find_python)

# ── 2. Create venv if absent ──────────────────────────────────────────────────
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "📦  Creating virtual environment …"
    "$PYTHON" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# ── 3. Install / upgrade dependencies ─────────────────────────────────────────
# Use requirements.lock if it exists (reproducible), else requirements.txt
if [ -f "$SCRIPT_DIR/requirements.lock" ]; then
    REQ_FILE="$SCRIPT_DIR/requirements.lock"
else
    REQ_FILE="$SCRIPT_DIR/requirements.txt"
fi

# Only install if requirements changed (compare checksum)
CHECKSUM_FILE="$VENV_DIR/.req_checksum"
CURRENT_HASH=$(md5 -q "$REQ_FILE" 2>/dev/null || md5sum "$REQ_FILE" 2>/dev/null | cut -d' ' -f1 || echo "")
STORED_HASH=$(cat "$CHECKSUM_FILE" 2>/dev/null || echo "")

if [ "$CURRENT_HASH" != "$STORED_HASH" ]; then
    echo "📦  Installing/updating dependencies …"
    "$VENV_PIP" install -q --upgrade pip
    "$VENV_PIP" install -q -r "$REQ_FILE"
    echo "$CURRENT_HASH" > "$CHECKSUM_FILE"
fi

# ── 4. Load .env if present ───────────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── 5. Dispatch ───────────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"

CMD="${1:-}"

case "$CMD" in
    # New ledger CLI commands (Form B via ledger_agent.cli.main)
    scan|s|balance|b|tax|t|form1065|f1|k1|k|reconcile|r)
        exec "$VENV_PYTHON" -m ledger_agent.cli.main "$@"
        ;;
    # Legacy pass-through to main.py
    ""|menu|mcp|context|classify|memory|summary|setup|import|transactions|onboard|o)
        exec "$VENV_PYTHON" main.py "$@"
        ;;
    --version|-v)
        "$VENV_PYTHON" -c "from ledger_agent import __version__; print('ledger-agent', __version__)"
        ;;
    --help|-h)
        "$VENV_PYTHON" -m ledger_agent.cli.main --help
        ;;
    *)
        # Try ledger CLI first, fall back to main.py
        exec "$VENV_PYTHON" -m ledger_agent.cli.main "$@" 2>/dev/null \
            || exec "$VENV_PYTHON" main.py "$@"
        ;;
esac

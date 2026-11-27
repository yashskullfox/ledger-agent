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
#   ./run.sh summary [YEAR] [--no-prompt]
#
# Usage (legacy main.py pass-through):
#   ./run.sh                          # interactive menu (also: ./run.sh menu)
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
    for py in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$py" &>/dev/null; then
            if "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
                echo "$py"; return
            fi
        fi
    done
    echo >&2 "ERROR: Python >= $PYTHON_MIN not found. Install it and retry."
    exit 1
}
# POSIX venvs use bin/python; Windows venvs (e.g. under Git Bash/MSYS) use
# Scripts/python.exe.
_venv_python() {
    if [ -f "$VENV_DIR/bin/python" ]; then
        echo "$VENV_DIR/bin/python"
    elif [ -f "$VENV_DIR/Scripts/python.exe" ]; then
        echo "$VENV_DIR/Scripts/python.exe"
    fi
}

# ── 2. Create venv if absent ──────────────────────────────────────────────────
VENV_PYTHON="$(_venv_python)"
if [ -z "$VENV_PYTHON" ]; then
    PYTHON=$(_find_python)
    echo "📦  Creating virtual environment …"
    "$PYTHON" -m venv "$VENV_DIR"
    VENV_PYTHON="$(_venv_python)"
    if [ -z "$VENV_PYTHON" ]; then
        echo >&2 "ERROR: virtual environment created but no interpreter found in $VENV_DIR"
        exit 1
    fi
fi

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
    "$VENV_PYTHON" -m pip install -q --upgrade pip
    "$VENV_PYTHON" -m pip install -q -r "$REQ_FILE"
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

# Keep this command table in sync with run.bat.
case "$CMD" in
    # New ledger CLI commands (Form B via ledger_agent.cli.main)
    scan|s|balance|b|tax|t|form1065|f1|k1|k|reconcile|r|summary|su)
        exec "$VENV_PYTHON" -m ledger_agent.cli.main "$@"
        ;;
    # Interactive menu (main.py with no arguments)
    ""|menu)
        exec "$VENV_PYTHON" main.py
        ;;
    # Legacy pass-through to main.py (commands + their main.py aliases)
    mcp|context|ctx|classify|c|memory|m|setup|init|import|i|transactions|tx|onboard|o|mom|bs|taxes)
        exec "$VENV_PYTHON" main.py "$@"
        ;;
    --version|-v)
        exec "$VENV_PYTHON" -c "from ledger_agent import __version__; print('ledger-agent', __version__)"
        ;;
    --help|-h)
        exec "$VENV_PYTHON" -m ledger_agent.cli.main --help
        ;;
    *)
        # Unknown command: let the ledger CLI report it (prints usage, exits 1).
        # Errors are not hidden and there is no silent fallback to main.py.
        exec "$VENV_PYTHON" -m ledger_agent.cli.main "$@"
        ;;
esac

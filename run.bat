@echo off
:: run.bat  –  ledger-agent bootstrap launcher for Windows (ARCH-05)
:: ==================================================================
:: Creates / activates a Python venv, installs dependencies, and runs
:: the ledger CLI or any legacy main.py command.
::
:: Usage (new ledger commands):
::   run.bat scan [FOLDER] [--no-prompt] [--allow-partial]
::   run.bat balance [YEAR]
::   run.bat tax     [YEAR]
::   run.bat form1065 [YEAR]
::   run.bat k1 [YEAR] [--partner partner_1^|partner_2]
::   run.bat reconcile [YEAR]
::
:: Usage (legacy main.py pass-through):
::   run.bat mcp                      :: start MCP stdio server
::   run.bat context 2025-01          :: export AI context JSON
::   run.bat classify                 :: batch-classify transactions
::
:: Environment:
::   FI_STATEMENTS_DIR   Override default statements folder
::   FI_DB_PATH          Override SQLite database path
::   FI_AI_BACKEND       local | openai | gemini  (default: local)

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
:: Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%" %= # redaction: allow =%

set "VENV_DIR=%SCRIPT_DIR%\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"

:: ── 1. Locate Python ─────────────────────────────────────────────────────────
set "PYTHON="
for %%P in (python3.12 python3.11 python3.10 python3 python) do (
    if "!PYTHON!"=="" (
        where %%P >nul 2>&1
        if not errorlevel 1 (
            for /f "delims=" %%V in ('%%P -c "import sys; ok=1 if sys.version_info>=(3,10) else 0; print(ok)" 2^>nul') do (
                if "%%V"=="1" set "PYTHON=%%P"
            )
        )
    )
)
if "!PYTHON!"=="" (
    echo ERROR: Python ^>= 3.10 not found. Install it from https://python.org and retry.
    exit /b 1
)

:: ── 2. Create venv if absent ──────────────────────────────────────────────────
if not exist "%VENV_PYTHON%" (
    echo [ledger] Creating virtual environment ...
    !PYTHON! -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        exit /b 1
    )
)

:: ── 3. Install / upgrade dependencies ─────────────────────────────────────────
:: Use requirements.lock if it exists (reproducible), else requirements.txt
if exist "%SCRIPT_DIR%\requirements.lock" (
    set "REQ_FILE=%SCRIPT_DIR%\requirements.lock"
) else (
    set "REQ_FILE=%SCRIPT_DIR%\requirements.txt"
)

:: Only reinstall when requirements file changes (compare hash)
set "CHECKSUM_FILE=%VENV_DIR%\.req_checksum"
set "CURRENT_HASH="
for /f "delims=" %%H in ('certutil -hashfile "!REQ_FILE!" MD5 2^>nul ^| findstr /v "MD5\|CertUtil"') do (
    if "!CURRENT_HASH!"=="" set "CURRENT_HASH=%%H"
)
:: Trim spaces from hash
set "CURRENT_HASH=!CURRENT_HASH: =!"

set "STORED_HASH="
if exist "%CHECKSUM_FILE%" (
    for /f "delims=" %%S in (%CHECKSUM_FILE%) do set "STORED_HASH=%%S"
    set "STORED_HASH=!STORED_HASH: =!"
)

if not "!CURRENT_HASH!"=="!STORED_HASH!" (
    echo [ledger] Installing/updating dependencies ...
    "%VENV_PIP%" install -q --upgrade pip
    "%VENV_PIP%" install -q -r "!REQ_FILE!"
    if errorlevel 1 (
        echo ERROR: Dependency installation failed.
        exit /b 1
    )
    echo !CURRENT_HASH!> "%CHECKSUM_FILE%"
)

:: ── 4. Load .env if present ───────────────────────────────────────────────────
if exist "%SCRIPT_DIR%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%SCRIPT_DIR%\.env") do (
        set "LINE=%%A"
        :: Skip comments and blank lines
        if not "!LINE:~0,1!"=="#" (
            if not "!LINE!"=="" (
                set "%%A=%%B"
            )
        )
    )
)

:: ── 5. Dispatch ───────────────────────────────────────────────────────────────
cd /d "%SCRIPT_DIR%"

set "CMD=%~1"
if "%CMD%"=="" set "CMD=menu"

:: New ledger CLI commands
set "LEDGER_CMDS= scan s balance b tax t form1065 f1 k1 k reconcile r "
:: Check if CMD is a ledger CLI command
echo !LEDGER_CMDS! | findstr /i " %CMD% " >nul 2>&1
if not errorlevel 1 (
    "%VENV_PYTHON%" -m ledger_agent.cli.main %*
    exit /b !errorlevel!
)

:: Version / help flags
if /i "%CMD%"=="--version" (
    "%VENV_PYTHON%" -c "from ledger_agent import __version__; print('ledger-agent', __version__)"
    exit /b 0
)
if /i "%CMD%"=="-v" (
    "%VENV_PYTHON%" -c "from ledger_agent import __version__; print('ledger-agent', __version__)"
    exit /b 0
)
if /i "%CMD%"=="--help" (
    "%VENV_PYTHON%" -m ledger_agent.cli.main --help
    exit /b 0
)
if /i "%CMD%"=="-h" (
    "%VENV_PYTHON%" -m ledger_agent.cli.main --help
    exit /b 0
)

:: Legacy main.py pass-through (mcp, context, classify, memory, summary, etc.)
set "LEGACY_CMDS= menu mcp context classify memory summary setup import transactions onboard o "
echo !LEGACY_CMDS! | findstr /i " %CMD% " >nul 2>&1
if not errorlevel 1 (
    "%VENV_PYTHON%" main.py %*
    exit /b !errorlevel!
)

:: Unknown command — try ledger CLI first, fall back to main.py
"%VENV_PYTHON%" -m ledger_agent.cli.main %* 2>nul
if errorlevel 1 (
    "%VENV_PYTHON%" main.py %*
)
exit /b !errorlevel!

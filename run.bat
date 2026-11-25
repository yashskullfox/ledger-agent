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
::   run.bat summary [YEAR] [--no-prompt]
::
:: Usage (legacy main.py pass-through):
::   run.bat                          :: interactive menu (also: run.bat menu)
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

:: ── 1. Locate Python ─────────────────────────────────────────────────────────
set "PYTHON="
for %%P in (python3.14 python3.13 python3.12 python3.11 python3.10 python3 python py) do (
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
    rem Use python -m pip rather than pip.exe, which cannot upgrade itself on Windows
    "%VENV_PYTHON%" -m pip install -q --upgrade pip
    "%VENV_PYTHON%" -m pip install -q -r "!REQ_FILE!"
    if errorlevel 1 (
        echo ERROR: Dependency installation failed.
        exit /b 1
    )
    echo !CURRENT_HASH!> "%CHECKSUM_FILE%"
)

:: ── 4. Load .env if present ───────────────────────────────────────────────────
::    Mirrors run.sh (which sources .env): KEY=VALUE lines, "#" comment lines
::    and blank lines skipped, optional "export " prefix, surrounding "..." or
::    '...' quotes stripped, and inline " #" comments removed from values.
::    Limitation: values containing "!" are mangled by delayed expansion.
set DQ=^"
if exist "%SCRIPT_DIR%\.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%SCRIPT_DIR%\.env") do (
        set "_K=%%A"
        set "_V=%%B"
        if /i "!_K:~0,7!"=="export " set "_K=!_K:~7!"
        set "_K=!_K: =!"
        rem Trim leading whitespace from the value
        if defined _V for /f "eol=` tokens=*" %%T in ("!_V!") do set "_V=%%T"
        rem Strip an inline space-hash comment unless the value is fully quoted
        set "_F=!_V:~0,1!"
        set "_L=!_V:~-1!"
        set "_QUOTED="
        if defined _V if "!_F!"=="!DQ!" if "!_L!"=="!DQ!" set "_QUOTED=1"
        if defined _V if "!_F!"=="'" if "!_L!"=="'" set "_QUOTED=1"
        if not defined _QUOTED if defined _V if not "!_V: #=!"=="!_V!" (
            set "_V=!_V: #=`!"
            for /f "eol=` tokens=1 delims=`" %%C in ("!_V!") do set "_V=%%C"
        )
        rem Trim trailing whitespace
        for /l %%N in (1,1,16) do if defined _V if "!_V:~-1!"==" " set "_V=!_V:~0,-1!"
        rem Strip matching surrounding quotes
        set "_F=!_V:~0,1!"
        set "_L=!_V:~-1!"
        if defined _V if "!_F!"=="!DQ!" if "!_L!"=="!DQ!" set "_V=!_V:~1,-1!"
        if defined _V if "!_F!"=="'" if "!_L!"=="'" set "_V=!_V:~1,-1!"
        if defined _K set "!_K!=!_V!"
    )
)
set "_K=" & set "_V=" & set "_F=" & set "_L=" & set "_QUOTED="

:: ── 5. Dispatch ───────────────────────────────────────────────────────────────
:: Keep this command table in sync with run.sh.
cd /d "%SCRIPT_DIR%"

set "CMD=%~1"

:: Interactive menu (main.py with no arguments)
if "%CMD%"=="" (
    "%VENV_PYTHON%" main.py
    exit /b !errorlevel!
)
if /i "%CMD%"=="menu" (
    "%VENV_PYTHON%" main.py
    exit /b !errorlevel!
)

set "ROUTE="
:: New ledger CLI commands (Form B via ledger_agent.cli.main)
for %%C in (scan s balance b tax t form1065 f1 k1 k reconcile r summary su) do (
    if /i "%CMD%"=="%%C" set "ROUTE=ledger"
)
:: Legacy pass-through to main.py (commands + their main.py aliases)
for %%C in (mcp context ctx classify c memory m setup init import i transactions tx onboard o mom bs taxes) do (
    if /i "%CMD%"=="%%C" set "ROUTE=legacy"
)

if "!ROUTE!"=="legacy" (
    "%VENV_PYTHON%" main.py %*
    exit /b !errorlevel!
)

:: Version / help flags
if /i "%CMD%"=="--version" set "ROUTE=version"
if /i "%CMD%"=="-v" set "ROUTE=version"
if "!ROUTE!"=="version" (
    "%VENV_PYTHON%" -c "from ledger_agent import __version__; print('ledger-agent', __version__)"
    exit /b !errorlevel!
)
if /i "%CMD%"=="--help" set "ROUTE=help"
if /i "%CMD%"=="-h" set "ROUTE=help"
if "!ROUTE!"=="help" (
    "%VENV_PYTHON%" -m ledger_agent.cli.main --help
    exit /b !errorlevel!
)

:: Ledger CLI commands, and unknown commands (the ledger CLI reports them,
:: prints usage and exits 1; errors are not hidden).
"%VENV_PYTHON%" -m ledger_agent.cli.main %*
exit /b !errorlevel!

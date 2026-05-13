"""
config.py  –  Global configuration for FinancialIntelligence
─────────────────────────────────────────────────────────────
All tuneable constants are loaded from environment variables.
Never hard-code secrets, API keys, or personal data in this file.

Environment variables (prefix: FI_):
  FI_DB_PATH                Override SQLite database path
  FI_DATA_DIR               Override data directory
  FI_AUTO_CLASSIFY_THRESHOLD  Fuzzy match threshold (0-100, default 85)
  FI_LOG_LEVEL              Logging level: DEBUG/INFO/WARNING/ERROR (default INFO)
  FI_LOG_FORMAT             Logging format: json/rich/plain (default rich)
  FI_AI_BACKEND             AI backend: local/openai/gemini (default local)
  FI_OPENAI_API_KEY         OpenAI API key (required if FI_AI_BACKEND=openai)
  FI_GEMINI_API_KEY         Gemini API key (required if FI_AI_BACKEND=gemini)
  FI_OPENAI_MODEL           OpenAI model (default gpt-4o-mini)
  FI_GEMINI_MODEL           Gemini model (default gemini-1.5-flash)
  FI_MEMORY_FILE            Override classification memory JSON path
  FI_DEFAULT_ENTITY_NAME    Pre-fill entity name in setup wizard
  FI_DEFAULT_ENTITY_STATE   Pre-fill entity state in setup wizard
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


# ── Helpers ───────────────────────────────────────────────────────────────────


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "")
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _env_path(key: str, default: Path) -> Path:
    raw = os.environ.get(key, "")
    return Path(raw) if raw.strip() else default


def _assert_no_hardcoded_secrets() -> None:
    """
    Scan this file at import time for common secret patterns.
    Raises RuntimeError if any are found (catches accidental commits).
    """
    _BAD_PATTERNS = [
        r"sk-[A-Za-z0-9]{20,}",  # OpenAI key
        r"AIza[A-Za-z0-9_\-]{35}",  # Google/Gemini key
        r"xoxb-[A-Za-z0-9\-]{50,}",  # Slack bot token
        r"ghp_[A-Za-z0-9]{36,}",  # GitHub PAT
        r"(?i)password\s*=\s*['\"][^'\"]{6,}",  # hardcoded password
    ]
    src = Path(__file__).read_text()
    for pat in _BAD_PATTERNS:
        if re.search(pat, src):
            raise RuntimeError(
                f"[config.py] Hardcoded secret detected (pattern: {pat!r}). "
                "Use environment variables instead."
            )


_assert_no_hardcoded_secrets()

# ── Root paths ────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).parent.resolve()
DATA_DIR = _env_path("FI_DATA_DIR", ROOT_DIR / "data")
STATEMENTS_DIR = DATA_DIR / "statements"
DB_DIR = DATA_DIR / "db"
EXPORTS_DIR = DATA_DIR / "exports"

for _d in (STATEMENTS_DIR, DB_DIR, EXPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = _env_path("FI_DB_PATH", DB_DIR / "financials.db")

# ── Entity defaults (editable via first-run wizard) ──────────────────────────
DEFAULT_ENTITY_NAME = _env_str("FI_DEFAULT_ENTITY_NAME", "")
DEFAULT_ENTITY_TYPE = _env_str("FI_DEFAULT_ENTITY_TYPE", "LLC")
DEFAULT_ENTITY_STATE = _env_str("FI_DEFAULT_ENTITY_STATE", "")

# ── Accounting settings ───────────────────────────────────────────────────────
DEFAULT_CURRENCY = "USD"
FISCAL_YEAR_START_MM = _env_int("FI_FISCAL_YEAR_START_MM", 1)

# ── Parser confidence thresholds ─────────────────────────────────────────────
AUTO_CLASSIFY_THRESHOLD = _env_int("FI_AUTO_CLASSIFY_THRESHOLD", 85)


# AI backend minimum confidence (0.0–1.0) to auto-apply a classification.
# The local backend returns scores on a 0.0–1.0 scale; the memory module
# uses 0–100.  Tunable via FI_LOCAL_CONFIDENCE_THRESHOLD.
def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


LOCAL_CONFIDENCE_THRESHOLD: float = _env_float("FI_LOCAL_CONFIDENCE_THRESHOLD", 0.75)

# ── Intelligence / memory ────────────────────────────────────────────────────
MEMORY_FILE = _env_path("FI_MEMORY_FILE", DB_DIR / "classification_memory.json")

# ── AI Backend ────────────────────────────────────────────────────────────────
AI_BACKEND = _env_str("FI_AI_BACKEND", "local").lower()  # local | openai | gemini
OPENAI_MODEL = _env_str("FI_OPENAI_MODEL", "gpt-4o-mini")
GEMINI_MODEL = _env_str("FI_GEMINI_MODEL", "gemini-1.5-flash")


def ai_api_key() -> Optional[str]:
    """Return the API key for the configured AI backend, or None."""
    if AI_BACKEND == "openai":
        return _env_str("FI_OPENAI_API_KEY") or None
    if AI_BACKEND == "gemini":
        return _env_str("FI_GEMINI_API_KEY") or None
    return None


def validate_ai_config() -> None:
    """Raise ValueError if the selected AI backend is missing its key."""
    if AI_BACKEND in ("openai", "gemini") and not ai_api_key():
        var = "FI_OPENAI_API_KEY" if AI_BACKEND == "openai" else "FI_GEMINI_API_KEY"
        raise ValueError(
            f"FI_AI_BACKEND={AI_BACKEND!r} requires {var} to be set. "
            "Add it to your .env file (never commit .env to version control)."
        )


# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = _env_str("FI_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = _env_str("FI_LOG_FORMAT", "rich").lower()  # rich | json | plain

# ── Report settings ───────────────────────────────────────────────────────────
REPORT_DATE_FMT = "%B %d, %Y"
REPORT_PERIOD_FMT = "%Y-%m"

# ── Supported statement parsers (human-readable labels) ─────────────────────
# This dict is informational (used for UI display and validation messages).
# The authoritative registration lives in each parser module via
# ParserRegistry.register(), auto-loaded by parsers/__init__.py.
KNOWN_PARSERS = {
    "truist_checking": "Truist Simple Business Checking",
    "fidelity_brokerage": "Fidelity Brokerage / Investment Account",
    "chase_checking": "Chase Business Complete Checking",
    "bofa_checking": "Bank of America Business Checking",
    "usbank_checking": "U.S. Bank Business Checking",
    "usbank_creditcard": "U.S. Bank Business Credit Card",
    "ibkr": "Interactive Brokers Activity Statement",
}

# ── Chart-of-Accounts account-type labels ────────────────────────────────────
ACCOUNT_TYPE_LABELS = {
    "asset": "Assets",
    "liability": "Liabilities",
    "equity": "Members' Equity",
    "revenue": "Revenue",
    "expense": "Expenses",
}

"""
config.py  –  Global configuration for FinancialIntelligence
─────────────────────────────────────────────────────────────
All tuneable constants are loaded from environment variables.
Never hard-code secrets, API keys, or personal data in this file.

Environment variables (prefix: FI_):
  FI_DB_PATH                Override SQLite database path
  FI_DATA_DIR               Override data directory
  FI_AUTO_CLASSIFY_THRESHOLD      Fuzzy match threshold (0-100, default 85)
  FI_LOCAL_CONFIDENCE_THRESHOLD   Chain escalation threshold (0.0-1.0, default 0.65)
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
  FI_SE_TAX_RATE            Self-employment tax rate (default 0.153)
  FI_FED_INCOME_RATE        Federal effective income tax rate (default 0.22)
  FI_STATE_TAX_RATE         State income tax rate (default 0.05)
  FI_QBI_DEDUCTION          QBI deduction rate (default 0.20)
  FI_STATEMENTS_DIR         Override default statements folder (data/statements/)
  FI_STATEMENT_GLOB         Glob pattern for statement files (default *.pdf)
  FI_AI_EGRESS_MODE         PII firewall: redact|strict|mock|passthrough (default redact)
  FI_AI_EGRESS_MODE_ACK     Required for passthrough: I_understand_the_risk
  FI_PRIVACY_ENTITY_NAME    Legal entity name to redact as <ENTITY_NAME> in AI payloads
  FI_PRIVACY_NER            Enable spaCy NER: spacy (optional, default off)
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


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "")
    try:
        return float(raw)
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
STATEMENTS_DIR = _env_path("FI_STATEMENTS_DIR", DATA_DIR / "statements")
DB_DIR = DATA_DIR / "db"
EXPORTS_DIR = DATA_DIR / "exports"

# Glob pattern for statement discovery (R-45)
STATEMENT_GLOB = _env_str("FI_STATEMENT_GLOB", "*.pdf")

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

# Minimum local-backend confidence before the chain escalates to remote AI.
# Range 0.0–1.0.  Higher = fewer API calls (more local-only).  Default: 0.65.
LOCAL_CONFIDENCE_THRESHOLD: float = _env_float("FI_LOCAL_CONFIDENCE_THRESHOLD", 0.65)

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
    "bank_x_checking":   "Bank X Simple Business Checking",
    "bank_x2_checking":  "Bank X2 Business Checking",
    "bank_x3_checking":  "Bank X3 Business Complete Checking",
    "bank_x4_checking":  "Bank X4 Business Essentials Checking",
    "bank_x4_creditcard":"Bank X4 Business Credit Card",
    "broker_y_brokerage":"Broker Y Brokerage / Investment Account",
    "broker_z":          "Broker Z Activity Statement",
}

# ── Chart-of-Accounts account-type labels ────────────────────────────────────
ACCOUNT_TYPE_LABELS = {
    "asset": "Assets",
    "liability": "Liabilities",
    "equity": "Members' Equity",
    "revenue": "Revenue",
    "expense": "Expenses",
}

# ── Privacy / Egress Control (R-46) ──────────────────────────────────────────
# redact     – apply PII tokenisation (default, recommended)
# strict     – redact + post-sweep that fails if any digit-run ≥ 7 remains
# mock       – short-circuit: never call remote; return stub classification
# passthrough – no redaction; requires FI_AI_EGRESS_MODE_ACK=I_understand_the_risk
AI_EGRESS_MODE = _env_str("FI_AI_EGRESS_MODE", "redact").lower()
AI_EGRESS_MODE_ACK = _env_str("FI_AI_EGRESS_MODE_ACK", "")

# Legal entity name to token-replace as <ENTITY_NAME> in all outbound AI payloads.
# Set to your LLC / business name so it never leaves the machine in the clear.
PRIVACY_ENTITY_NAME = _env_str("FI_PRIVACY_ENTITY_NAME", "")

# Optional: set to 'spacy' to activate spaCy NER for higher-quality person detection.
# Requires: pip install spacy && python -m spacy download en_core_web_sm
PRIVACY_NER = _env_str("FI_PRIVACY_NER", "").lower()

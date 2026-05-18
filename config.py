"""
config  –  Compatibility shim (W7-CORRUPTED-IMPORTS)
─────────────────────────────────────────────────────
The canonical home is `ledger_agent/core/config.py`. This top-level module
exists only so legacy `from config import …` and `import config as _cfg`
call-sites in the codebase keep working without a wide refactor.

Do NOT add new symbols here — add them in `ledger_agent/core/config.py` and
they will be re-exported automatically via the star-import below.

Why a star-import + explicit re-binds?
  • `from X import *` only re-exports names without a leading underscore.
  • The mutation pattern `import config as _cfg; _cfg.DB_PATH = …` (used in
    tests/conftest.py to redirect to a tmp DB) requires that the names live
    on THIS module object, not on the canonical one. The star-import binds
    them here, which is exactly what the mutation pattern needs.
"""
from __future__ import annotations

# Re-export every public symbol from the canonical config module.
from ledger_agent.core.config import *  # noqa: F401,F403

# A few internal-style names that __all__ would omit but legacy code uses.
from ledger_agent.core.config import (  # noqa: F401
    ROOT_DIR,
    DATA_DIR,
    DB_DIR,
    DB_PATH,
    STATEMENTS_DIR,
    STATEMENT_GLOB,
    EXPORTS_DIR,
    MEMORY_FILE,
    AUTO_CLASSIFY_THRESHOLD,
    LOCAL_CONFIDENCE_THRESHOLD,
    AI_BACKEND,
    OPENAI_MODEL,
    GEMINI_MODEL,
    ai_api_key,
    validate_ai_config,
    LOG_LEVEL,
    LOG_FORMAT,
    REPORT_DATE_FMT,
    REPORT_PERIOD_FMT,
    KNOWN_PARSERS,
    ACCOUNT_TYPE_LABELS,
    AI_EGRESS_MODE,
    AI_EGRESS_MODE_ACK,
    PRIVACY_ENTITY_NAME,
    PRIVACY_NER,
    DEFAULT_ENTITY_NAME,
    DEFAULT_ENTITY_TYPE,
    DEFAULT_ENTITY_STATE,
    DEFAULT_CURRENCY,
    FISCAL_YEAR_START_MM,
)

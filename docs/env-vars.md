# Environment Variable Reference

All runtime configuration for ledger-agent is driven by environment variables.
Copy `.env.example` → `.env` and fill in your values.

## Python core (`FI_` prefix)

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `FI_DATA_DIR` | `data/` | No | Override the data directory root |
| `FI_DB_PATH` | `data/db/financials.db` | No | Override SQLite database path |
| `FI_MEMORY_FILE` | `data/db/classification_memory.json` | No | Override classification memory file |
| `FI_STATEMENTS_DIR` | `data/statements/` | No | Override statements directory for scan/onboard |
| `FI_STATEMENT_GLOB` | `*.pdf` | No | Glob pattern for statement discovery |
| `FI_RAW_CACHE_DIR` | `data/raw_cache/` | No | Override raw parser cache directory |
| `FI_EXPORT_TMP_DIR` | `data/exports/_tmp/` | No | Override export scratch directory |
| `FI_DEFAULT_ENTITY_NAME` | `Entity` | No | Default entity name when none exists |
| `FI_DEFAULT_ENTITY_STATE` | _(none)_ | No | Default entity state code (e.g. `MO`) |
| `FI_DEFAULT_ENTITY_TYPE` | `LLC` | No | Default entity type |
| `FI_AI_BACKEND` | `local` | No | AI backend: `local`, `openai`, `gemini` |
| `FI_OPENAI_API_KEY` | _(none)_ | When `FI_AI_BACKEND=openai` | OpenAI API key |
| `FI_OPENAI_MODEL` | `gpt-4o-mini` | No | OpenAI model name |
| `FI_GEMINI_API_KEY` | _(none)_ | When `FI_AI_BACKEND=gemini` | Google Gemini API key |
| `FI_GEMINI_MODEL` | `gemini-1.5-flash` | No | Gemini model name |
| `FI_AUTO_CLASSIFY_THRESHOLD` | `85` | No | Fuzzy match score for auto-classify (0–100) |
| `FI_LOCAL_CONFIDENCE_THRESHOLD` | `0.65` | No | AI confidence threshold for escalation (0.0–1.0) |
| `FI_LOG_LEVEL` | `INFO` | No | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `FI_LOG_FORMAT` | `rich` | No | Log format: `rich`, `json`, `plain` |
| `FI_FISCAL_YEAR_START_MM` | `1` | No | Fiscal year start month (1 = January) |
| `FI_SE_TAX_RATE` | `0.153` | No | Self-employment tax rate |   # redaction: allow
| `FI_FED_INCOME_RATE` | `0.22` | No | Federal income tax effective rate |   # redaction: allow
| `FI_STATE_TAX_RATE` | `0.048` | No | State income tax rate |   # redaction: allow
| `FI_QBI_DEDUCTION` | `0.20` | No | QBI pass-through deduction (IRC §199A) |   # redaction: allow
| `FI_AI_EGRESS_MODE` | `redact` | No | PII egress mode: `redact`, `strict`, `mock`, `passthrough` |
| `FI_AI_EGRESS_MODE_ACK` | _(none)_ | When mode=`passthrough` | Risk acknowledgement string |
| `FI_PRIVACY_ENTITY_NAME` | _(none)_ | No | Legal entity name to redact in AI payloads |
| `FI_PRIVACY_NER` | _(none)_ | No | NER backend: `spacy` (optional, requires spaCy install) |
| `FI_AUDIT_DIR` | `data/audit/` | No | Override audit log directory |
| `FI_AUDIT_RETENTION_DAYS` | `7` | No | Days to retain audit log files |
| `FI_AUDIT_DISABLED` | `0` | No | Set to `1` to disable audit logging (not recommended) |
| `FI_PARTNER_1_NAME` | `Partner A` | No | Display name for partner_1 |
| `FI_PARTNER_1_CAPITAL` | `0.99` | No | Capital allocation % for partner_1 |   # redaction: allow
| `FI_PARTNER_1_PL` | `1.00` | No | P&L allocation % for partner_1 |   # redaction: allow
| `FI_PARTNER_2_NAME` | `Partner B` | No | Display name for partner_2 |
| `FI_PARTNER_2_CAPITAL` | `0.01` | No | Capital allocation % for partner_2 |   # redaction: allow
| `FI_PARTNER_2_PL` | `0.00` | No | P&L allocation % for partner_2 |   # redaction: allow
| `FI_WASH_SALE_CSV` | `private/wash_sale_adjustments.csv` | No | Path to wash-sale disallowance CSV (W16) |
| `FI_PARTNER_1_CORPUS_TOKEN` | `partner 1` | No | Redaction token for partner_1 name in parity corpus regeneration |
| `FI_PARTNER_2_CORPUS_TOKEN` | `partner 2` | No | Redaction token for partner_2 name in parity corpus regeneration |

## Webapp (Form D — Spring Boot)

| Variable | Default | Description |
|----------|---------|-------------|
| `LEDGER_PYTHON_HOME` | _(system Python)_ | Path to Python interpreter for the bridge |
| `LEDGER_DEFAULT_FISCAL_YEAR` | `2024` | Default fiscal year shown in the UI |
| `LEDGER_APP_TITLE` | `Financial Intelligence` | Application title displayed in the UI |
| `LEDGER_APP_BADGE` | `Form D` | Badge label shown in the UI header |
| `LEDGER_PARTNER_A_LABEL` | `partner_1` | Display label for partner A in the UI |
| `LEDGER_PARTNER_B_LABEL` | `partner_2` | Display label for partner B in the UI |
| `PORT` | `8080` | HTTP port for the Spring Boot server |

## Notes

- All `FI_` variables are read by the Python core (`ledger_agent/core/`), CLI, and MCP server.
- `LEDGER_` variables are read by the Spring Boot webapp only.
- `FI_DB_PATH` is used by test fixtures to isolate test databases (see `tests/conftest.py`).
- `FI_CPA_CORPUS_PATH` is a test-only override for the parity corpus path; it is not documented
  in the table above because it has no effect outside the test suite.
- For the full PII egress firewall spec, see `AGENTS.md §3`.

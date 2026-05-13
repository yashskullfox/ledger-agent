# FinancialIntelligence — Architecture & Structure

> A field guide for contributors, AI agents, and developers extending the system.

---

## Philosophy

**Local compute first, API as validator, MCP as optional interface.**

1. All parsing, classification, and reporting runs entirely on the local machine with zero network dependency.
2. Remote AI (OpenAI, Gemini) is an optional second-pass validator for low-confidence classifications only — not the primary engine.
3. The MCP server exposes finished data to any MCP-compatible AI client; it does not replace the intelligence layer.
4. Every institution is a self-contained plugin. Adding a new bank requires exactly one file.

---

## Repository Layout

```
FinancialIntelligence/
│
├── main.py                  CLI entry point and command dispatcher
├── run.sh                   Shell launcher: manages .venv, passes args
├── config.py                All configuration via environment variables
├── pyproject.toml           PEP 621 package metadata and fi CLI entry point
├── requirements.txt         Runtime dependencies (no dev/test deps)
├── requirements-dev.txt     pytest, coverage
├── .env.example             Config template — copy to .env and fill in
├── .gitignore               Excludes financial data, keys, caches, dev artifacts
│
├── core/                    Pure domain layer — no I/O, no CLI, no DB calls
│   ├── models.py            Dataclass models: Entity, Account, Transaction, Position …
│   ├── database.py          SQLite repositories (EntityRepo, TransactionRepo, …)
│   ├── exceptions.py        Custom exception hierarchy
│   └── logging_setup.py     Structured logging: rich | json | plain
│
├── parsers/                 Statement PDF parser plugins
│   ├── __init__.py          Auto-discovery via pkgutil.iter_modules — no manual registration
│   ├── base.py              BaseStatementParser ABC + shared helpers
│   ├── registry.py          @ParserRegistry.register decorator and auto-detect
│   ├── truist_checking.py   Truist Simple Business Checking
│   ├── fidelity_brokerage.py  Fidelity Investment Report
│   ├── chase_checking.py    Chase Business Complete Checking
│   ├── bofa_checking.py     Bank of America Business Checking
│   ├── usbank_checking.py   U.S. Bank Business Essentials Checking
│   ├── usbank_creditcard.py U.S. Bank Business Credit Card
│   └── ibkr.py              Interactive Brokers Activity Statement
│
├── intelligence/            Classification and learning layer
│   ├── classifier.py        5-step pipeline: memory → local → AI → keywords → prompt
│   ├── memory.py            JSON-backed persistent classification rules
│   ├── reconciler.py        Inter-account transfer matching
│   └── ai_backend/
│       ├── __init__.py      Factory: local | openai | gemini → ChainedBackend
│       ├── base.py          AIBackend abstract interface
│       ├── chained_backend.py  Local-first wrapper: runs local, escalates to remote
│       ├── local_backend.py Rule table + rapidfuzz fuzzy match (zero API cost)
│       ├── openai_backend.py   GPT-4o-mini via OpenAI Chat Completions
│       └── gemini_backend.py   Google Gemini 1.5 Flash
│
├── accounting/              Financial statement builders
│   ├── balance_sheet.py     BalanceSheetBuilder → GAAP-style balance sheet
│   └── tax_estimator.py     Quarterly 1040-ES estimator (SE + federal + state + QBI)
│
├── reports/
│   └── renderer.py          Rich console output + CSV / Excel / JSON export
│
├── adapters/
│   └── context_builder.py   Serialises financial data for Claude / GPT / Perplexity
│
├── mcp_server/              Optional MCP stdio server
│   ├── __init__.py
│   └── server.py            JSON-RPC 2.0 over stdio — no external dependencies
│
├── cli/
│   ├── commands.py          Command functions called by main.py
│   ├── quick_scan.py        ⚡ Folder → auto-import all PDFs → reports; delegates
│   │                        coverage check to onboarding wizard before importing
│   ├── onboarding.py        📅 R-45 coverage wizard: 12-month rolling gap analysis,
│   │                        interactive gap-fill loop, CI-safe --no-prompt mode
│   └── prompts.py           Interactive prompts (questionary + rich)
│
├── tests/
│   ├── conftest.py          Shared fixtures (in-memory SQLite)
│   ├── test_models.py
│   ├── test_parsers.py
│   ├── test_classifier.py
│   ├── test_balance_sheet.py
│   ├── test_tax_estimator.py
│   └── test_onboarding.py   R-45 coverage wizard unit tests
│
└── data/                    ← NOT committed (gitignored)
    ├── statements/          PDF statements
    ├── db/
    │   ├── financials.db    SQLite database
    │   └── classification_memory.json
    └── exports/             CSV / Excel / JSON exports
```

---

## Data Flow

```
Coverage Discovery (R-45: ./run.sh scan)
  │  Resolve folder → discover PDFs → probe each (parser + period + account)
  │  Build 12-month coverage matrix → render ✅/⚠/❌ table
  │  Gap-fill interactive loop → wait for missing months
  ▼
PDF file (each discovered statement)
   │
   ▼ parsers/registry.py → detect parser by can_parse() fingerprint
   │
   ▼ BaseStatementParser.parse() → ParsedStatement
   │   ├── transactions: List[Transaction]
   │   ├── positions:    List[Position]
   │   └── snapshot:     AccountSnapshot
   │
   ▼ cli/commands.py → persist to SQLite via core/database.py
   │
   ▼ intelligence/classifier.py → classify each transaction
   │   Step 1: parser pre-classification (e.g. IRS, Fees)
   │   Step 2: memory.lookup()      → rapidfuzz match against learned rules
   │   Step 3: AI backend           → local rules → (optionally) remote AI
   │   Step 4: COA keyword scan     → Chart of Accounts keyword table
   │   Step 5: interactive prompt   → user selects, result saved to memory
   │
   ▼ accounting/balance_sheet.py → BalanceSheet object
   │   Assets = bank snapshots + investment positions − margin
   │   Equity = Revenue − Expenses (from classified transactions)
   │
   ▼ reports/renderer.py → console / CSV / Excel / JSON
   │
   ▼ adapters/context_builder.py → ai_context_YYYY-MM.json
       paste into Claude, GPT, or connect via MCP
```

---

## Classification Pipeline Detail

```
classify_transaction(txn):
  1. Already classified? → return (parser pre-tagged it)
  2. memory.lookup(description)
       rapidfuzz WRatio ≥ AUTO_CLASSIFY_THRESHOLD (default 85) → auto-apply
  3. get_backend().classify_transaction(description, amount)
       LocalBackend:    rule regex → fuzzy → heuristic (confidence 0.20–0.90)
       ChainedBackend:  local first → remote AI only if local < 0.65
       MCP:             not in the classification chain (separate interface)
  4. COA keyword scan (single unambiguous keyword match → auto-apply)
  5. prompt_fn(txn, coa_entries) → user picks from list → saved to memory
  6. Fallback: code "9999" (Unclassified – Review Required)
```

---

## Adding a New Institution Parser

Create `parsers/wells_fargo.py`:

```python
from parsers.base import BaseStatementParser
from parsers.registry import ParserRegistry
from core.models import ParsedStatement, StatementType
from pathlib import Path

@ParserRegistry.register
class WellsFargoParser(BaseStatementParser):
    PARSER_ID   = "wells_fargo"
    INSTITUTION = "Wells Fargo"

    # Optional: improves gap-fill prompts in the onboarding coverage wizard
    EXPECTED_FILENAME_HINT = "wells_fargo_*_{period}*.pdf"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        return "WELLS FARGO" in text.upper() and "BUSINESS" in text.upper()

    def parse(self, pdf_path: Path) -> ParsedStatement:
        raw_text = self.extract_text(pdf_path)
        # ... extract period, account number, transactions ...
        return ParsedStatement(parser_id=self.PARSER_ID, ...)
```

That's it. `parsers/__init__.py` uses `pkgutil.iter_modules` to auto-discover every
module in the `parsers/` package at startup — the decorator registers the class and
`can_parse()` fingerprinting selects it automatically. **No edits to any other file
are needed.**

Steps in full:

1. Create `parsers/wells_fargo.py`
2. Subclass `BaseStatementParser`, implement `can_parse()` and `parse()`
3. Decorate with `@ParserRegistry.register`
4. Done — the file is auto-discovered on next run

Optionally add `EXPECTED_FILENAME_HINT = "wells_fargo_*_{period}*.pdf"` as a class
attribute to produce more helpful gap-fill prompts in the R-45 onboarding wizard.

---

## AI Backend Architecture

```
FI_AI_BACKEND=local   →  LocalBackend
                            regex rules → rapidfuzz → heuristic
                            Cost: $0.00  Speed: <1ms

FI_AI_BACKEND=openai  →  ChainedBackend(local → OpenAIBackend)
                            LocalBackend runs first
                            OpenAI only called when local confidence < 0.65
                            Cost: ~$0.00002 / uncertain transaction

FI_AI_BACKEND=gemini  →  ChainedBackend(local → GeminiBackend)
                            Same chain, Gemini replaces OpenAI
                            Cost: ~$0.000001 / uncertain transaction

MCP server            →  Separate interface; exposes finished data
                            Has no role in the classification chain
                            Any MCP client (Claude Desktop, Cursor, etc.)
                            connects via stdio JSON-RPC 2.0
```

**Threshold tuning:** Set `FI_LOCAL_CONFIDENCE_THRESHOLD` (default `0.65`) to control
how aggressively the system escalates to the remote AI.  Higher = fewer API calls.

---

## MCP Server Integration

The MCP server (`mcp_server/server.py`) is a pure-stdlib JSON-RPC 2.0 server
over stdio. It uses **MCP-spec newline-delimited JSON framing** (one JSON object
per line, no `Content-Length` headers), compatible with Claude Desktop, Cursor,
Cline, Continue, and the reference `mcp` Python SDK. It requires no external
dependencies and exposes six read-only tools to any MCP-compatible client. All
six tools are covered by smoke tests (subprocess-level: `test_initialize`,
`test_tools_list`, `test_list_periods_empty_db`).

**Claude Desktop config** (`~/.claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "financial-intelligence": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/FinancialIntelligence"
    }
  }
}
```

**Available MCP tools:**
| Tool | Description |
|---|---|
| `get_balance_sheet` | Full balance sheet for a YYYY-MM period |
| `list_transactions` | Transactions with COA codes, filterable by period |
| `get_tax_estimate` | Quarterly 1040-ES estimate from balance sheet |
| `classify_transaction` | COA suggestion for a description string |
| `list_periods` | All available statement periods |
| `get_entity_summary` | Entity name, accounts, period coverage |

---

## Configuration Reference

All configuration via environment variables. Copy `.env.example` to `.env`.

| Variable                        | Default                              | Purpose                                                |
|---------------------------------|--------------------------------------|--------------------------------------------------------|
| `FI_AI_BACKEND`                 | `local`                              | `local` / `openai` / `gemini`                          |
| `FI_LOCAL_CONFIDENCE_THRESHOLD` | `0.65`                               | Below this, local escalates to remote AI               |
| `FI_AUTO_CLASSIFY_THRESHOLD`    | `85`                                 | Memory fuzzy-match score to auto-apply (0–100)         |
| `FI_OPENAI_API_KEY`             | —                                    | Required if backend=openai                             |
| `FI_GEMINI_API_KEY`             | —                                    | Required if backend=gemini                             |
| `FI_OPENAI_MODEL`               | `gpt-4o-mini`                        | OpenAI model                                           |
| `FI_GEMINI_MODEL`               | `gemini-1.5-flash`                   | Gemini model                                           |
| `FI_DB_PATH`                    | `data/db/financials.db`              | SQLite database path                                   |
| `FI_DATA_DIR`                   | `data/`                              | Data root                                              |
| `FI_MEMORY_FILE`                | `data/db/classification_memory.json` | Learned rules                                          |
| `FI_STATEMENTS_DIR`             | `data/statements/`                   | Override default statements folder for coverage wizard |
| `FI_STATEMENT_GLOB`             | `*.pdf`                              | Glob pattern for statement discovery                   |
| `FI_LOG_LEVEL`                  | `INFO`                               | `DEBUG` / `INFO` / `WARNING` / `ERROR`                 |
| `FI_LOG_FORMAT`                 | `rich`                               | `rich` / `json` / `plain`                              |
| `FI_SE_TAX_RATE`                | `0.153`                              | Self-employment tax rate                               |
| `FI_FED_INCOME_RATE`            | `0.22`                               | Federal income tax rate                                |
| `FI_STATE_TAX_RATE`             | `0.05`                               | State income tax rate                                  |
| `FI_QBI_DEDUCTION`              | `0.20`                               | Qualified Business Income deduction                    |

---

## Key Design Decisions

| Decision                         | Rationale                                                                     |
|----------------------------------|-------------------------------------------------------------------------------|
| SQLite over Postgres             | Zero-infra, single-file, works on any laptop                                  |
| Decimal over float               | Exact monetary arithmetic, no IEEE 754 rounding                               |
| Plugin parser registry           | New banks require no changes to core code                                     |
| JSON classification memory       | Human-readable, diff-friendly, committable to git                             |
| Local-first AI                   | Small business runs free; cloud AI is opt-in validation                       |
| No hardcoded credentials         | Secret guard in config.py prevents accidental commits                         |
| MCP as interface only            | Financial data stays local; MCP exposes summaries only                        |
| Auto-discovering parser registry | Drop one file in parsers/ — pkgutil.iter_modules picks it up automatically    |
| Coverage wizard (R-45)           | 12-month rolling window shows gaps before importing; CI-safe --no-prompt mode |

---

## Running Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest                                    # 127 tests
pytest --cov=. --cov-report=term-missing  # with coverage
pytest tests/test_parsers.py -v           # single file
```

---

## Security Model

- `data/` is gitignored — statements, database, and exports never leave your machine
- `.env` is gitignored — API keys never committed
- `config.py` scans itself at import for accidentally committed keys (secret guard)
- Account numbers stored as last-4 masked strings only
- MCP server is local stdio only — no network exposure unless you proxy it
- When using remote AI backends (OpenAI/Gemini), transaction descriptions flow to the
  remote API. Account numbers are masked. Counterparty names and amounts are not
  currently redacted. R-46 will introduce a tokenizing redactor (in backlog).
- **R-46 (planned): Privacy firewall** — tokenizes PII in descriptions before outbound
  API calls, replacing counterparty names and sensitive terms with reversible tokens
  so raw descriptions never leave the machine.

---

*Last updated: May 2026*

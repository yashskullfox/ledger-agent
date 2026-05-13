# FinancialIntelligence

> Generic, extensible financial statement aggregator, balance sheet generator, and AI-assisted transaction classifier
> for small business entities (LLCs, sole props, S-Corps).

Built for real-world use with **Truist Bank** and **Fidelity Investments** statements, and extensible to any institution
via a plug-in parser registry.

---

## Features

| Feature                  | Description                                                                    |
|--------------------------|--------------------------------------------------------------------------------|
| 📄 **PDF Import**        | Auto-detect and parse bank & brokerage statements — no manual data entry       |
| 📊 **Balance Sheet**     | Full GAAP-style balance sheet: Assets, Liabilities, Members' Equity            |
| 💰 **Tax Estimator**     | Quarterly estimated tax payments (SE tax + federal + state)                    |
| 🤖 **AI Classification** | Local rules, OpenAI GPT-4o-mini, or Google Gemini (choose your backend)        |
| 🧠 **Memory / Learning** | Learns from your confirmations — gets smarter every month                      |
| 📅 **Coverage Wizard**   | 12-month gap analysis: Rich matrix, interactive gap-filling, then batch import |
| 🔌 **Extensible**        | Add any bank: one parser file — auto-discovered, no other edits needed         |
| 🔐 **Secure**            | Zero hardcoded secrets; all config via environment variables                   |
| 📤 **AI Context Export** | JSON export for Claude, GPT-4, Perplexity — ask questions about your finances  |

---

## Supported Institutions

| Institution          | Statement Type                | Parser               | Test Coverage       |
|----------------------|-------------------------------|----------------------|---------------------|
| Truist Bank          | Simple Business Checking      | `truist_checking`    | End-to-end          |
| Fidelity Investments | Brokerage / Investment Report | `fidelity_brokerage` | Detection only      |
| Chase Bank           | Business Complete Checking    | `chase_checking`     | Plugin scaffolded ¹ |
| Bank of America      | Business Checking             | `bofa_checking`      | Plugin scaffolded ¹ |
| U.S. Bank            | Business Essentials Checking  | `usbank_checking`    | Plugin scaffolded ¹ |
| U.S. Bank            | Business Credit Card          | `usbank_creditcard`  | Plugin scaffolded ¹ |
| Interactive Brokers  | Activity Statement            | `ibkr`               | Plugin scaffolded ¹ |

¹ *Parser exists and was manually validated against a real statement PDF, but does not yet have an automated `parse()`
unit test. Contributions welcome — see the testing guide in `STRUCTURE.md`.*

→ **Adding a new institution:** Create `parsers/my_bank.py`, subclass `BaseStatementParser`, decorate with
`@ParserRegistry.register`. That is all — `parsers/__init__.py` auto-discovers every module in the package via
`pkgutil.iter_modules`. No edits to `cli/commands.py`, `cli/quick_scan.py`, or any other file are needed.

---

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/your-org/financial-intelligence.git
cd financial-intelligence/FinancialIntelligence
./run.sh --install          # creates .venv and installs deps
```

### 2. Configure (optional)

```bash
cp .env.example .env
# Edit .env — set entity name, AI backend, tax rates, etc.
```

### 3. Run

```bash
./run.sh                    # interactive menu
./run.sh scan ~/Downloads/statements/   # coverage wizard + batch import
./run.sh onboard            # alias for scan
./run.sh import statement.pdf           # import a single PDF
./run.sh balance 2025-01                # view balance sheet
./run.sh tax 2025-01                    # view tax estimate
./run.sh context 2025-01               # export AI context JSON
```

---

## Modes

FinancialIntelligence operates in three modes, selected by `FI_AI_BACKEND`:

### Local Mode (default — no API key required)

```bash
FI_AI_BACKEND=local ./run.sh
```

Uses rule-based classification + [rapidfuzz](https://github.com/maxbachmann/RapidFuzz) fuzzy matching. Works offline.
Best for most small businesses.

### OpenAI Mode

```bash
FI_AI_BACKEND=openai FI_OPENAI_API_KEY=sk-... ./run.sh
# or set in .env
```

Uses GPT-4o-mini for intelligent transaction classification. Excellent for unusual vendors or complex descriptions.

> ⚠️ **Privacy note (R-46 in backlog):** When using the OpenAI or Gemini backends, raw transaction descriptions
> including counterparty names are sent to the remote API. R-46 (planned) will introduce a tokenizing redactor
> that strips counterparty names before transmission. Until then, review your privacy posture before enabling
> remote AI backends.

### Gemini Mode

```bash
FI_AI_BACKEND=gemini FI_GEMINI_API_KEY=AIza... ./run.sh
# or set in .env
```

Uses Google Gemini 1.5 Flash. Fast and cost-effective alternative to OpenAI.

> ⚠️ Same privacy caveat as OpenAI mode above.

---

## Commands

```
./run.sh [command] [options]

Commands:
  (none)               Interactive menu
  scan   [FOLDER]      Coverage wizard: discover PDFs, show gap matrix, import
  onboard [FOLDER]     Alias for scan (same coverage wizard)
  import [PDF]         Import a single statement PDF
  balance [PERIOD]     View balance sheet (e.g. 2025-01)
  tax    [PERIOD]      View tax obligation estimate
  context [PERIOD]     Export AI context JSON
  transactions [P]     View / export transactions
  classify             Classify unclassified transactions
  memory               View / manage learned classification rules
  summary              Month-over-month comparison table
  setup                Re-run entity setup wizard

Flags:
  --install                     Create .venv and install dependencies (first time)

Flags for scan / onboard:
  --force                       Re-import already-imported statements
  --no-prompt                   CI mode: emit JSON coverage matrix, no interactive prompts
  --window 2025-01:2025-12      Override the default 12-month rolling window
  --report                      Show balance sheet + tax estimate after ingestion
```

---

## Coverage Wizard (scan / onboard)

`scan` and `onboard` are the recommended entry point for monthly bookkeeping. They run the R-45 Coverage Wizard:

```bash
./run.sh scan ~/Documents/statements/
# or equivalently:
./run.sh onboard ~/Documents/statements/
```

The wizard:

1. **Resolves the folder** — CLI arg > `FI_STATEMENTS_DIR` env var > last-used cached path > `data/statements/`
2. **Discovers all PDFs** recursively (glob controlled by `FI_STATEMENT_GLOB`, default `*.pdf`)
3. **Renders a Rich coverage matrix** — one row per account, one column per month:
    - ✅ present (exactly one statement found)
    - ⚠ duplicate (more than one statement found for that month)
    - ❌ missing
4. **Prompts interactively for each missing month:**
    - `[w]` Wait — polls the folder every 2 s for up to 5 min, then continues when the file appears
    - `[s]` Skip — skip this month and continue
    - `[a]` Skip all — skip all remaining gaps and proceed to import
    - `[q]` Quit — exit without importing
5. **Ingests all found statements** after gap-filling

### CI / non-interactive mode

```bash
./run.sh scan FOLDER --no-prompt
```

- Emits a JSON coverage matrix to stdout
- No interactive prompts (safe in scripts and CI pipelines)
- Exit code `0` = all months present; `2` = gaps remain

Example JSON output:

```json
{
  "window": ["2024-05", "2024-06", "...", "2025-04"],
  "accounts": [{"key": "Truist Bank|1234", "institution": "Truist Bank", "last4": "1234"}],
  "matrix": {
    "Truist Bank ****1234": {"2024-05": "present", "2024-06": "missing", "...": "..."}
  },
  "missing_count": 1,
  "complete": false
}
```

---

## Balance Sheet Structure

```
ASSETS
  Current Assets
    Truist Bank – Business Checking      $4,031.20
  Investment Assets
    Fidelity – Brokerage Account
      SNAP  ×3,000 @ $11.2900          $33,870.00
    Gross Securities Holdings            $33,870.00
    Less: Margin Loan                  ($24,061.20)
    Net Investment Assets                $9,808.80
  ───────────────────────────────────────────────
  TOTAL ASSETS                          $37,901.20

LIABILITIES
  Margin Loan Payable                   $24,061.20
  ───────────────────────────────────────────────
  TOTAL LIABILITIES                     $24,061.20

MEMBERS' EQUITY
  Retained Earnings (Prior Periods)      $3,457.85
  Net Income – 2025-01                  $10,382.15
  ───────────────────────────────────────────────
  TOTAL MEMBERS' EQUITY                 $13,840.00

TOTAL LIABILITIES + EQUITY             $37,901.20  ✓ BALANCED
```

> **Note on "BALANCED":** Members' Equity is derived as `Total Assets − Total Liabilities`,
> so the balance equation holds by construction. This is a structural sanity check, not a
> full double-entry journal validation. A proper audit trail requires a journal ledger (backlog R-40).

---

## Tax Estimator

Estimates quarterly IRS Form 1040-ES payments based on:

- **Self-employment tax** (15.3% of net SE income)
- **Federal income tax** (~22% effective rate, adjustable)
- **State income tax** (5% default, configurable)
- **QBI deduction** (20% of qualified business income)
- **Annualization** — extrapolates from a single month to annual

Rates are configurable via environment variables (see `.env.example`).

> ⚠️ These are estimates only. Consult a CPA for actual tax filings.

---

## AI Context Export (GitHub / AI Consumption)

Export your financial data as a structured JSON file for any AI assistant:

```bash
./run.sh context 2025-01
# → Saved: data/exports/ai_context_2025-01.json
```

Then paste the content (or a summary) into Claude, GPT-4, or Perplexity:

```
"Here is my financial data for January 2025:
[paste ai_context_2025-01.json content]

Questions:
- What is my biggest expense category?
- Are my quarterly tax payments on track?
- How does my net income compare to last month?"
```

The context file includes: balance sheet, transactions, positions, tax estimate, and an AI system prompt.

---

## Transaction Classification

Each transaction goes through a 5-step classification pipeline:

1. **Pre-classified by parser** — some parsers tag obvious transactions (IRS, fees, transfers)
2. **Memory lookup** — exact + fuzzy match against learned rules (rapidfuzz WRatio)
3. **AI backend suggestion** — local rules, OpenAI, or Gemini
4. **COA keyword scan** — checks Chart of Accounts keywords
5. **Interactive prompt** — shows multiple-choice menu for unknowns

Confirmed classifications are saved to `data/db/classification_memory.json` and improve over time.

### Committing Your Memory File

After classifying a batch, you may commit the memory file to version control to preserve learned rules across
reinstalls and share them with team members:

```bash
git add data/db/classification_memory.json
git commit -m "chore: update classification memory"
```

> ⚠️ **Privacy caveat:** Classification patterns are derived from transaction descriptions and may contain
> counterparty names (e.g. vendor or payee names). Review the file before committing to a shared or public
> repository. R-46 (privacy firewall, in backlog) will make this git-safe by construction by redacting
> counterparty names from stored patterns before they are written to disk.

---

## Chart of Accounts

The system seeds a 28-entry COA covering typical LLC/trading company accounts:

| Code      | Name                    | Type       |
|-----------|-------------------------|------------|
| 1000-1999 | Assets                  | Asset      |
| 2000-2999 | Liabilities             | Liability  |
| 3000-3999 | Members' Equity         | Equity     |
| 4000-4099 | Revenue                 | Revenue    |
| 5000-5999 | Expenses                | Expense    |
| 9000      | Inter-Account Transfers | (Internal) |

---

## Project Structure

```
FinancialIntelligence/
├── main.py                    # CLI entry point
├── run.sh                     # Launch script (manages .venv)
├── config.py                  # Environment-variable config
├── .env.example               # Config template (copy to .env)
├── .gitignore                 # Git exclusions
├── pyproject.toml             # PEP 621 package metadata
├── requirements.txt           # Runtime dependencies
├── requirements-dev.txt       # Dev/test dependencies
│
├── core/
│   ├── models.py              # Data models (Entity, Transaction, …)
│   ├── database.py            # SQLite repositories
│   ├── exceptions.py          # Custom exceptions
│   └── logging_setup.py       # Structured logging (rich/json/plain)
│
├── parsers/
│   ├── __init__.py            # Auto-discovery via pkgutil.iter_modules
│   ├── base.py                # BaseStatementParser ABC
│   ├── registry.py            # Parser auto-detection registry
│   ├── truist_checking.py     # Truist Simple Business Checking
│   ├── fidelity_brokerage.py  # Fidelity Investment Report
│   ├── chase_checking.py      # Chase Business Complete Checking
│   ├── bofa_checking.py       # Bank of America Business Checking
│   ├── usbank_checking.py     # U.S. Bank Business Essentials Checking
│   ├── usbank_creditcard.py   # U.S. Bank Business Credit Card
│   └── ibkr.py               # Interactive Brokers Activity Statement
│
├── intelligence/
│   ├── classifier.py          # 5-step classification pipeline
│   ├── memory.py              # Persistent classification rules
│   ├── reconciler.py          # Inter-account transfer matching
│   └── ai_backend/
│       ├── __init__.py        # Backend factory (get_backend())
│       ├── base.py            # AIBackend ABC
│       ├── chained_backend.py # Local-first chain: local → remote on low confidence
│       ├── local_backend.py   # Rule-based + rapidfuzz (default, $0.00)
│       ├── openai_backend.py  # OpenAI GPT-4o-mini (optional escalation)
│       └── gemini_backend.py  # Google Gemini 1.5 Flash (optional escalation)
│
├── accounting/
│   ├── balance_sheet.py       # BalanceSheetBuilder
│   └── tax_estimator.py       # Quarterly tax obligation estimator
│
├── reports/
│   └── renderer.py            # Rich console + CSV/Excel/JSON export
│
├── adapters/
│   └── context_builder.py     # AI-consumable JSON context builder
│
├── mcp_server/
│   └── server.py              # MCP stdio server (MCP-spec newline-delimited JSON-RPC 2.0)
│
├── cli/
│   ├── commands.py            # Command implementations (import, balance, classify, …)
│   ├── onboarding.py          # R-45: 12-month Coverage Wizard (scan / onboard)
│   ├── quick_scan.py          # Single-PDF import helper used by onboarding
│   └── prompts.py             # Interactive prompts (questionary/rich)
│
├── tests/
│   ├── conftest.py            # Shared fixtures
│   ├── test_models.py         # Model unit tests
│   ├── test_parsers.py        # Parser unit tests
│   ├── test_classifier.py     # Classifier + AI backend tests
│   ├── test_balance_sheet.py  # Balance sheet builder tests
│   ├── test_tax_estimator.py  # Tax estimator tests
│   └── test_onboarding.py     # Coverage Wizard tests (44 tests, R-45)
│
└── data/                      # ← NOT committed to git
    ├── statements/            # PDF statements go here
    ├── db/
    │   ├── financials.db      # SQLite database
    │   └── classification_memory.json
    └── exports/               # CSV / Excel / JSON exports
```

---

## Running Tests

```bash
# Install dev deps
pip install -r requirements.txt -r requirements-dev.txt

# Run all tests (127 total)
pytest

# With coverage
pytest --cov=. --cov-report=term-missing

# Run specific file
pytest tests/test_parsers.py -v
pytest tests/test_onboarding.py -v
```

---

## Environment Variables Reference

| Variable                        | Default                              | Description                                                |
|---------------------------------|--------------------------------------|------------------------------------------------------------|
| `FI_AI_BACKEND`                 | `local`                              | AI backend: `local` / `openai` / `gemini`                  |
| `FI_OPENAI_API_KEY`             | —                                    | OpenAI API key (required if backend=openai)                |
| `FI_GEMINI_API_KEY`             | —                                    | Gemini API key (required if backend=gemini)                |
| `FI_OPENAI_MODEL`               | `gpt-4o-mini`                        | OpenAI model name                                          |
| `FI_GEMINI_MODEL`               | `gemini-1.5-flash`                   | Gemini model name                                          |
| `FI_AUTO_CLASSIFY_THRESHOLD`    | `85`                                 | Fuzzy match score for auto-classification (0–100)          |
| `FI_LOCAL_CONFIDENCE_THRESHOLD` | `0.65`                               | Below this, local backend escalates to remote AI (0.0–1.0) |
| `FI_DB_PATH`                    | `data/db/financials.db`              | SQLite database path                                       |
| `FI_DATA_DIR`                   | `data/`                              | Data root directory                                        |
| `FI_MEMORY_FILE`                | `data/db/classification_memory.json` | Classification memory                                      |
| `FI_STATEMENTS_DIR`             | `data/statements/`                   | Override the default statements folder for scan/onboard    |
| `FI_STATEMENT_GLOB`             | `*.pdf`                              | Glob pattern for PDF discovery (e.g. `*statement*.pdf`)    |
| `FI_LOG_LEVEL`                  | `INFO`                               | Logging level: `DEBUG`/`INFO`/`WARNING`/`ERROR`            |
| `FI_LOG_FORMAT`                 | `rich`                               | Log format: `rich`/`json`/`plain`                          |
| `FI_SE_TAX_RATE`                | `0.153`                              | Self-employment tax rate (15.3%)                           |
| `FI_FED_INCOME_RATE`            | `0.22`                               | Federal income tax estimate rate                           |
| `FI_STATE_TAX_RATE`             | `0.05`                               | State income tax estimate rate                             |
| `FI_QBI_DEDUCTION`              | `0.20`                               | Qualified Business Income deduction (20%)                  |
| `FI_DEFAULT_ENTITY_NAME`        | —                                    | Pre-fill entity name in setup wizard                       |
| `FI_DEFAULT_ENTITY_STATE`       | —                                    | Pre-fill entity state in setup wizard                      |

---

## Security

- **No secrets in code**: All API keys and sensitive config use environment variables
- **Secret guard**: `config.py` scans itself at import for accidentally committed keys
- **`.gitignore`**: Excludes `data/`, `.env`, `*.db`, `*.pdf` — financial data never commits
- **Masked account numbers**: Stored as `****1234` — never store full account numbers
- **R-46 (planned — privacy firewall):** When using OpenAI or Gemini backends, raw transaction descriptions
  including counterparty names are sent to the remote API. R-46 will introduce a tokenizing redactor that
  strips counterparty names before any data leaves the machine.

---

## MCP Server

FinancialIntelligence includes an MCP (Model Context Protocol) stdio server that exposes your financial data
directly to Claude Desktop, Cursor, Cline, Continue, and other MCP-compatible clients.

```bash
python -m mcp_server.server
```

**Framing:** The server uses MCP-spec newline-delimited JSON (one JSON object per line), not LSP
`Content-Length` headers. This matches the reference MCP Python SDK and Claude Desktop expectations.

**Exposed tools (all functional):**

| Tool                   | Description                                                |
|------------------------|------------------------------------------------------------|
| `get_balance_sheet`    | Full balance sheet for a given `YYYY-MM` period            |
| `list_transactions`    | Transactions list, optionally filtered by period and limit |
| `get_tax_estimate`     | Quarterly tax obligation estimate for a period             |
| `classify_transaction` | Classify a single transaction description on-the-fly       |
| `list_periods`         | All statement periods available in the database            |
| `get_entity_summary`   | Entity name, account list, and period coverage             |

**Claude Desktop config** (`~/.claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "financial-intelligence": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/FinancialIntelligence"
    }
  }
}
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
    PARSER_ID = "wells_fargo"
    INSTITUTION = "Wells Fargo"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        return "WELLS FARGO" in text.upper() and "BUSINESS" in text.upper()

    def parse(self, pdf_path: Path) -> ParsedStatement:
        raw_text = self.extract_text(pdf_path)
        # ... extract period, account number, transactions ...
        return ParsedStatement(
            parser_id=self.PARSER_ID,
            statement_type=StatementType.BANK_CHECKING,
            # ...
        )
```

That is all. `parsers/__init__.py` uses `pkgutil.iter_modules` to auto-discover every module in the
`parsers/` package at import time. No changes to `cli/commands.py`, `cli/quick_scan.py`, `main.py`,
or any other file are needed.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Disclaimer

This software is for personal financial organization only. It is not a substitute for professional accounting, tax, or
investment advice. Always consult a qualified CPA for tax filings and a licensed financial advisor for investment
decisions.

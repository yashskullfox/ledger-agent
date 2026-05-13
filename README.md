# ledger-agent

> Four-form financial intelligence platform for SYNCED LLC.
> One source tree → Core library · CLI · MCP server · Spring Boot webapp.

---

## Four Forms at a Glance

| Form                       | What it is                                                                                              | Entry point                                       | Install                               |
|----------------------------|---------------------------------------------------------------------------------------------------------|---------------------------------------------------|---------------------------------------|
| **A — Core library**       | Pure-Python engine: parsers, classifiers, accounting, tax, reporting. No UI, no network.                | `import ledger_agent.core.api`                    | `pip install ledger-agent-core`       |
| **B — CLI runner**         | Interactive terminal UI; reads a folder, prompts when needed, writes CSV/JSON exports.                  | `ledger` shell command · `./run.sh`               | `pip install ledger-agent-cli`        |
| **C — MCP server**         | Spec-compliant MCP (stdio + HTTP). Privacy firewall on every egress.                                    | `ledger-agent-mcp` · `python -m ledger_agent.mcp` | `pip install ledger-agent-mcp`        |
| **D — Spring Boot webapp** | Self-contained fat jar — Spring Boot + embedded Python bridge. No install needed on the user's machine. | `java -jar ledger-agent-webapp.jar`               | download fat jar from GitHub Releases |

All four forms are built from this single repository and produce **identical numbers** for every report.

---

## Quick Start

### Form B — CLI (most common)

```bash
# Unix
git clone https://github.com/your-org/ledger-agent.git
cd ledger-agent
./run.sh scan ~/Downloads/statements/     # coverage wizard + batch import
./run.sh balance 2024                     # year-end balance sheet
./run.sh form1065 2024                    # Form 1065 partnership return
./run.sh k1 2024 --partner yash           # Schedule K-1
./run.sh tax 2024                         # quarterly tax estimate
./run.sh reconcile 2024                   # inter-account reconciliation

# Windows
run.bat scan C:\Users\you\statements
```

### Form C — MCP server (AI agents / Claude Desktop)

```bash
pip install ledger-agent-mcp
ledger-agent-mcp                          # stdio transport (default)
ledger-agent-mcp --transport http         # streamable-HTTP on :7337
```

**Claude Desktop config** (`~/.claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "ledger-agent": {
      "command": "ledger-agent-mcp"
    }
  }
}
```

### Form D — Spring Boot webapp

```bash
java -jar ledger-agent-webapp-v2.1.0-linux-x86_64.jar
# → opens http://localhost:8080 in your browser
```

No Python, Maven, or any other dependency needed on the user's machine.

---

## Six Core Operations

All four forms expose the same six operations (mapped to `ledger_agent.core.api`):

| Operation             | CLI         | MCP tool                 | Java bridge                     |
|-----------------------|-------------|--------------------------|---------------------------------|
| Import PDF statements | `scan`      | `import_statements`      | `bridge.importStatements()`     |
| Balance sheet         | `balance`   | `generate_balance_sheet` | `bridge.generateBalanceSheet()` |
| Form 1065             | `form1065`  | `generate_form_1065`     | `bridge.generateForm1065()`     |
| Schedule K-1          | `k1`        | `generate_k1`            | `bridge.generateK1()`           |
| PTE tax estimate      | `tax`       | `pte_estimate`           | `bridge.pteEstimate()`          |
| Reconciliation        | `reconcile` | `reconcile_year`         | `bridge.reconcileYear()`        |

---

## CLI Reference (Form B)

```
ledger <command> [args] [flags]

Commands
  scan   [FOLDER]           Coverage wizard + batch PDF import
  balance [YEAR]            Year-end balance sheet (default: 2024)
  form1065 [YEAR]           Form 1065 partnership return summary
  k1 [YEAR]                 Schedule K-1 (--partner yash|parin)
  tax    [YEAR]             Quarterly PTE tax estimate
  reconcile [YEAR]          Inter-account transfer reconciliation

Aliases: s b f1 k t r

Flags
  --no-prompt               CI mode (no interactive prompts, JSON to stdout)
  --allow-partial           Skip R-45 12-month completeness gate
  --partner yash|parin      Partner for k1 command

Legacy pass-through (main.py)
  mcp  context  classify  memory  summary  setup  import  transactions
```

---

## MCP Tools (Form C)

Six tools, one per core operation. Privacy firewall (R-46) applied to every response:

| Tool                     | Description                                          |
|--------------------------|------------------------------------------------------|
| `import_statements`      | Scan folder for PDFs, parse and persist. Idempotent. |
| `generate_balance_sheet` | GAAP-style year-end balance sheet.                   |
| `generate_form_1065`     | Form 1065 partnership return data.                   |
| `generate_k1`            | Schedule K-1 for `yash` (99%) or `parin` (1%).       |
| `pte_estimate`           | Quarterly estimated tax payments + due dates.        |
| `reconcile_year`         | Inter-account transfer reconciliation.               |

Pass `_meta: { allow_pii: true }` in a tool call to bypass the PII redaction filter.

---

## Environment Variables

| Variable                        | Default                 | Description                                       |
|---------------------------------|-------------------------|---------------------------------------------------|
| `FI_AI_BACKEND`                 | `local`                 | `local` / `openai` / `gemini`                     |
| `FI_DB_PATH`                    | `data/db/financials.db` | SQLite database path                              |
| `FI_STATEMENTS_DIR`             | `data/statements/`      | Default statements folder                         |
| `FI_AI_EGRESS_MODE`             | `redact`                | `redact` / `strict` / `mock` / `passthrough`      |
| `FI_OPENAI_API_KEY`             | —                       | Required if backend=openai                        |
| `FI_GEMINI_API_KEY`             | —                       | Required if backend=gemini                        |
| `FI_AUTO_CLASSIFY_THRESHOLD`    | `85`                    | Fuzzy match score for auto-classification         |
| `FI_LOCAL_CONFIDENCE_THRESHOLD` | `0.65`                  | Escalation threshold to remote AI                 |
| `FI_SE_TAX_RATE`                | `0.153`                 | Self-employment tax rate                          |
| `FI_FED_INCOME_RATE`            | `0.22`                  | Federal income tax estimate rate                  |
| `FI_STATE_TAX_RATE`             | `0.05`                  | Missouri PTE tax rate                             |
| `FI_QBI_DEDUCTION`              | `0.20`                  | Qualified Business Income deduction               |
| `LEDGER_PYTHON_HOME`            | —                       | Python home for Form D fat jar runtime extraction |

---

## Running Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt

# Full suite (207 tests)
pytest

# Architecture purity (zero CLI/UI imports in core)
pytest tests/architecture/test_core_purity.py -q

# MCP privacy firewall
pytest tests/integration/test_mcp_privacy.py -q

# CPA parity gate (requires private corpus)
FI_CPA_CORPUS_PATH=statements/2024.txt \
  pytest -m parity tests/integration/test_2024_cpa_parity.py -q
```

---

## Supported Institutions

| Institution          | Statement Type                | Parser               |
|----------------------|-------------------------------|----------------------|
| Truist Bank          | Simple Business Checking      | `truist_checking`    |
| Fidelity Investments | Brokerage / Investment Report | `fidelity_brokerage` |
| Chase Bank           | Business Complete Checking    | `chase_checking`     |
| Bank of America      | Business Checking             | `bofa_checking`      |
| U.S. Bank            | Business Essentials Checking  | `usbank_checking`    |
| U.S. Bank            | Business Credit Card          | `usbank_creditcard`  |
| Interactive Brokers  | Activity Statement            | `ibkr`               |

**Adding a new institution:** create `parsers/my_bank.py`, subclass `BaseStatementParser`,
decorate with `@ParserRegistry.register`. The parser is auto-discovered — no other edits needed.

---

## Security & Privacy

- **R-46 PII firewall** — all MCP egress passes through `core/privacy.py`; account numbers, SSNs,
  EINs, and partner names are redacted before any response leaves the host.
- **R-45 completeness gate** — year-end outputs refuse to generate when a month × institution is
  missing unless `--allow-partial` is passed.
- No secrets in code — all config via environment variables.
- `data/` is gitignored — statements, database, and exports never commit.

---

## Release Pipeline

Every merge to `main` triggers `.github/workflows/release.yml` which builds all four artifacts,
runs the CPA parity gate, and publishes a tagged GitHub Release:

| Artifact                              | Form |
|---------------------------------------|------|
| `ledger-agent-core-vX.Y.Z.zip`        | A    |
| `ledger-agent-cli-vX.Y.Z.tar.gz`      | B    |
| `ledger-agent-mcp-vX.Y.Z.zip`         | C    |
| `ledger-agent-webapp-vX.Y.Z-{os}.jar` | D    |

Semver is computed automatically from conventional commits since the last tag.

---

## License

MIT — see [LICENSE](LICENSE).

> This software is for personal financial organization only. It is not a substitute for
> professional accounting, tax, or investment advice. Always consult a qualified CPA.

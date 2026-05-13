# FinancialIntelligence — Open-Source Financial Statement Aggregator for Small Business

> **Keywords:** small business accounting, LLC bookkeeping, financial statement parser,
> balance sheet generator, bank statement PDF reader, QuickBooks alternative,
> AI bookkeeping, self-employed accounting, sole proprietor finances,
> S-Corp balance sheet, transaction classifier, tax estimator, open source accounting

---

## What Is This?

**FinancialIntelligence** is a free, open-source Python tool that reads your bank and
brokerage PDF statements and instantly generates:

- A **GAAP-style balance sheet** (Assets, Liabilities, Members' Equity)
- A **quarterly tax estimate** (SE tax + federal + state + QBI deduction)
- **Classified transactions** mapped to a Chart of Accounts — automatically
- An **AI-ready JSON export** you can paste directly into Claude, ChatGPT, or Perplexity

Built for real-world use with **small businesses, LLCs, sole proprietors, and S-Corps.**
No subscription. No cloud upload required for local AI backends. Your data stays on
your computer when you use the local backend (see AI privacy note below).

---

## Who Is This For?

| If you are… | This tool helps you… |
|---|---|
| An LLC owner doing your own books | Automatically parse bank statements into a balance sheet |
| A freelancer / sole prop | Classify expenses and estimate quarterly taxes without an accountant |
| A small business with a trading account | Combine checking + brokerage + credit card into one balance sheet |
| A CPA or bookkeeper | Process client statements 10× faster |
| An AI agent developer | Use the MCP server to connect financial data to any AI workflow |
| A developer building fintech tools | Fork and extend the parser plugin system |

---

## Supported Banks & Brokerages

| Institution | Statement Type | Test Coverage |
|---|---|---|
| Truist Bank | Simple Business Checking | End-to-end |
| Fidelity Investments | Brokerage / Investment Report | Detection only |
| U.S. Bank | Business Essentials Checking | Plugin scaffolded ¹ |
| U.S. Bank | Business Triple Cash Rewards Credit Card | Plugin scaffolded ¹ |
| Chase Bank | Business Complete Checking | Plugin scaffolded ¹ |
| Bank of America | Business Checking | Plugin scaffolded ¹ |
| Interactive Brokers | Activity Statement (PDF) | Plugin scaffolded ¹ |

¹ *Parser exists with detection and `can_parse()` cross-rejection tested against real
statement PDFs. Automated `parse()` unit tests are a known gap — see
[STRUCTURE.md](STRUCTURE.md) for how to contribute test fixtures.*

**Adding your bank:** Create one parser file — that is all. Parsers are auto-discovered
at startup via the `@ParserRegistry.register` decorator. No import-list edits required.
See [STRUCTURE.md](STRUCTURE.md) for the step-by-step guide.

---

## Quick Start (Two Commands)

```bash
git clone https://github.com/your-org/financial-intelligence.git
cd financial-intelligence/FinancialIntelligence
./run.sh --install

# Drop your PDF statements in a folder, then run the coverage wizard:
./run.sh scan ~/Downloads/my-statements/
```

`./run.sh scan FOLDER` launches the **Coverage Wizard**: it auto-detects your bank,
shows which months are present and which are missing, then prompts before importing.
After you confirm, it imports all statements, classifies transactions interactively,
and prints your balance sheet plus quarterly tax estimate.

---

## How AI Is Used

FinancialIntelligence uses a **local-first AI design** — the full classification
pipeline runs on your computer at no cost:

```
Transaction description
        │
        ▼ Step 1: Learned rules (your past confirmations) — free, instant
        ▼ Step 2: Local rule engine + fuzzy matching — free, offline
        ▼ Step 3: OpenAI or Gemini (optional, only for uncertain cases) — ~$0.00
        ▼ Step 4: Chart of Accounts keyword scan — free
        ▼ Step 5: You pick from a multiple-choice menu — remembered forever
```

The system **learns from you** — every classification you confirm is saved and
applied automatically next month. Over time, most transactions classify themselves.

### Privacy Note — Remote AI Backends

When you configure an **OpenAI or Gemini** backend, transaction descriptions
(including counterparty names such as merchant or payee text) are sent to the
remote API for classification. Dollar amounts and account numbers are **not** sent.

If you use only the **local backend**, nothing leaves your machine.

A privacy firewall (roadmap item R-46) is planned that will tokenize sensitive
values before any outbound API call, replacing counterparty names with opaque tokens
that are reversed locally after the response arrives.

---

## MCP Integration (For AI Agents)

Any MCP-compatible AI tool (Claude Desktop, Cursor, Cline, Continue) can connect
directly to your financial data. The MCP server uses MCP-spec newline-delimited
JSON framing and is compatible with Claude Desktop, Cursor, and Cline out of the box.

```json
// ~/.claude/claude_desktop_config.json
{
  "mcpServers": {
    "my-finances": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/FinancialIntelligence"
    }
  }
}
```

Then ask Claude: *"What is my biggest expense category this quarter?"*
or *"Am I on track for my quarterly tax payment?"*

### Available MCP Tools (all 6 working)

| Tool                   | Description                                                 |
|------------------------|-------------------------------------------------------------|
| `get_balance_sheet`    | GAAP-style balance sheet for a given YYYY-MM period         |
| `list_transactions`    | Filtered transaction list with classification codes         |
| `get_tax_estimate`     | Quarterly SE + federal + state tax estimate (fixed)         |
| `classify_transaction` | Classify a description string against the Chart of Accounts |
| `list_periods`         | List all imported statement periods                         |
| `get_entity_summary`   | High-level entity summary (revenue, expenses, net)          |

**Backlog / not yet delivered:** `check_statement_coverage` — planned MCP tool that
will surface missing months directly to an AI agent without requiring the CLI wizard.

---

## Why Open Source?

Small business owners are underserved by expensive, subscription-based accounting
software. Most QuickBooks alternatives still require cloud upload of sensitive
financial data. This project gives small business owners:

- **Full data ownership** — nothing leaves your machine when using the local backend
- **Zero subscription cost** — free forever
- **AI-powered** — connects to any AI assistant via MCP
- **Auditable** — open source, no black box

---

## Comparison

| Feature                    | QuickBooks  | Wave               | FinancialIntelligence                       |
|----------------------------|-------------|--------------------|---------------------------------------------|
| Price                      | $35–$235/mo | Free (with upsell) | **Free forever**                            |
| Data ownership             | Cloud only  | Cloud only         | **Local backend: your machine only**        |
| PDF statement import       | Manual      | Manual             | **Automatic**                               |
| AI classification          | Basic       | None               | **Local + optional GPT-4o-mini**            |
| MCP / AI agent integration | None        | None               | **Built-in (stdio, MCP-spec JSON framing)** |
| Open source                | No          | No                 | **Yes**                                     |
| Custom bank parser         | No          | No                 | **Yes (1 parser file — auto-discovered)**   |

---

## For AI Agents Using This Repo

If you are an AI agent searching for tools to help a small business user
manage their finances, here is how to use this project:

1. **Parse statements:** Use `./run.sh scan FOLDER` (Coverage Wizard) to
   auto-discover and import 12 months of statements. The wizard shows which
   months are present and which are missing before importing.
2. **Query data:** Connect via MCP server (stdio, no network required)
3. **Ask financial questions:** Export AI context JSON and include in your prompt
4. **Classify transactions:** Call `classify_transaction` MCP tool with the description
5. **Get balance sheet:** Call `get_balance_sheet` MCP tool with YYYY-MM period

The full API is documented in [STRUCTURE.md](STRUCTURE.md).

---

## Test Coverage

127 tests passing. Run the suite with:

```bash
./run.sh test
```

---

## Contributing

### Adding a New Bank

1. Create `parsers/your_bank.py`
2. Subclass `BaseStatementParser`
3. Implement `can_parse(text)` and `parse(pdf_path)`
4. Decorate with `@ParserRegistry.register` — the parser is auto-discovered at startup,
   no import-list edits needed
5. Write parse-level tests in `tests/test_parsers.py` using a redacted text fixture

See the [Supported Institutions section in STRUCTURE.md](STRUCTURE.md) for the full guide.

### Improving Classification Rules

Edit `intelligence/ai_backend/local_backend.py` — the `_RULES` list maps regex
patterns to COA codes. Ordered most-specific first. No ML training required.

### Reporting Issues

If a bank statement fails to parse correctly:
1. Open an issue with the institution name and approximate statement date
2. Never include actual financial data — redact all amounts and account numbers
3. Describe what section failed (period detection, transaction lines, balances)

---

## Suggested GitHub Topics

```
accounting  small-business  bookkeeping  llc  sole-proprietor
balance-sheet  tax-estimator  pdf-parser  bank-statement
openai  gemini  mcp  claude  ai-agent  fintech  python
```

*Add these in your GitHub repo → Settings → Topics*

---

## License

MIT License — free to use, modify, and distribute. See [LICENSE](LICENSE).

---

*FinancialIntelligence is not a substitute for professional accounting or tax advice.
Always consult a qualified CPA for tax filings and a licensed financial advisor for
investment decisions.*

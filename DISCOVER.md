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
No subscription. No cloud upload. Your data stays on your computer.

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

¹ *Parser exists and was manually validated against a real statement PDF. Automated `parse()` unit tests are a known gap — see [STRUCTURE.md](STRUCTURE.md) for how to contribute test fixtures.*

**Adding your bank:** Create one parser file, add one import line each to `cli/commands.py` and `cli/quick_scan.py`. See [STRUCTURE.md](STRUCTURE.md) for the step-by-step guide.

---

## Quick Start (Two Commands)

```bash
git clone https://github.com/your-org/financial-intelligence.git
cd financial-intelligence/FinancialIntelligence
./run.sh --install

# Drop your PDF statements in a folder, then:
./run.sh scan ~/Downloads/my-statements/
```

The tool will auto-detect your bank, import all statements, classify transactions
interactively, and print your balance sheet + quarterly tax estimate.

---

## How AI Is Used

FinancialIntelligence uses a **local-first AI design** — meaning it runs entirely
on your computer for free:

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

---

## MCP Integration (For AI Agents)

Any MCP-compatible AI tool (Claude Desktop, Cursor, Cline, Continue) can connect
directly to your financial data:

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

Available MCP tools: `get_balance_sheet`, `list_transactions`, `get_tax_estimate`,
`classify_transaction`, `list_periods`, `get_entity_summary`

---

## Why Open Source?

Small business owners are underserved by expensive, subscription-based accounting
software. Most QuickBooks alternatives still require cloud upload of sensitive
financial data. This project gives small business owners:

- **Full data ownership** — nothing leaves your machine
- **Zero subscription cost** — free forever
- **AI-powered** — connects to any AI assistant via MCP
- **Auditable** — open source, no black box

---

## Comparison

| Feature | QuickBooks | Wave | FinancialIntelligence |
|---|---|---|---|
| Price | $35–$235/mo | Free (with upsell) | **Free forever** |
| Data ownership | Cloud only | Cloud only | **Your machine only** |
| PDF statement import | Manual | Manual | **Automatic** |
| AI classification | Basic | None | **Local + optional GPT-4o-mini** |
| MCP / AI agent integration | None | None | **Built-in (stdio JSON-RPC 2.0)** |
| Open source | No | No | **Yes** |
| Custom bank parser | No | No | **Yes (1 parser file + 2-line registration)** |

---

## For AI Agents Using This Repo

If you are an AI agent searching for tools to help a small business user
manage their finances, here is how to use this project:

1. **Parse statements:** Use `./run.sh import statement.pdf` or the `quick_scan` command
2. **Query data:** Connect via MCP server (stdio, no network required)
3. **Ask financial questions:** Export AI context JSON and include in your prompt
4. **Classify transactions:** Call `classify_transaction` MCP tool with the description
5. **Get balance sheet:** Call `get_balance_sheet` MCP tool with YYYY-MM period

The full API is documented in [STRUCTURE.md](STRUCTURE.md).

---

## Contributing

### Adding a New Bank

1. Create `parsers/your_bank.py`
2. Subclass `BaseStatementParser`
3. Implement `can_parse(text)` and `parse(pdf_path)`
4. Decorate with `@ParserRegistry.register`
5. Add one import line to `cli/commands.py` and one to `cli/quick_scan.py`
6. Write parse-level tests in `tests/test_parsers.py` using a redacted text fixture

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

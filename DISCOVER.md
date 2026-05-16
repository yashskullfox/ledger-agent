# ledger-agent — Financial Intelligence Platform for Small Business

> **Keywords:** small business accounting, LLC bookkeeping, financial statement parser,
> balance sheet generator, bank statement PDF reader, MCP server, Spring Boot webapp,
> AI bookkeeping, quarterly tax estimator, Form 1065, Schedule K-1, Python CLI,
> Model Context Protocol, open source accounting, partnership accounting

---

## What Is This?

**ledger-agent** is a free, open-source financial intelligence platform that ships in
**four interchangeable forms** from a single Python source tree:

| Form | Run it as… | Use case |
|------|-----------|---------|
| **Core library** | `import ledger_agent.core.api` | Other developers, AI agents, integrations |
| **CLI runner** | `ledger balance 2024` or `./run.sh` | Founders, accountants on a laptop |
| **MCP server** | `ledger-agent-mcp` | Claude Desktop, Cursor, Wibey, any MCP-aware IDE |
| **Spring Boot webapp** | `java -jar ledger-agent.jar` | Non-CLI users, demos, team sharing |

All four forms produce **identical numbers** — one core engine, four surfaces.

---

## What It Generates

From your PDF bank and brokerage statements, ledger-agent produces:

- **GAAP-style balance sheet** — Assets, Liabilities, Members' Equity, Net Income
- **Form 1065 summary** — Ordinary Business Income, Schedule K items, partnership totals
- **Schedule K-1** — each partner's distributive share (configurable ownership split)
- **Quarterly tax estimate (PTE)** — SE tax + federal + state + QBI, with due dates
- **Inter-account reconciliation** — matches intra-bank transfers / Zelle / wire transfers
- **Classified transactions** — mapped to Chart of Accounts automatically via 5-step pipeline
- **AI-ready JSON context** — paste into Claude, GPT-4, or Perplexity

---

## Who Is This For?

| If you are… | This tool helps you… |
|---|---|
| An LLC owner doing your own books | Auto-parse bank statements, generate Form 1065 and K-1s |
| A founder tracking a trading + checking account | Combine brokerage + bank into one GAAP balance sheet |
| A CPA processing client statements | Run the full year-end workflow in one command |
| An AI agent / LLM IDE user | Connect financial data via MCP — six tools, privacy firewall included |
| A developer building fintech tools | Fork the parser plugin system; one file per institution |

---

## Quick Start (Two Commands)

```bash
# Unix — CLI runner
./run.sh scan ~/Downloads/statements/   # coverage wizard + import
./run.sh balance 2024                   # year-end balance sheet

# Windows
run.bat scan C:\Users\you\statements
run.bat balance 2024

# MCP server (drop into Claude Desktop)
pip install ledger-agent-mcp
ledger-agent-mcp

# Spring Boot webapp (no Python needed)
java -jar ledger-agent-webapp-v2.1.0-linux-x86_64.jar
# → http://localhost:8080
```

---

## Six Core Operations

Every form exposes these six operations (all producing identical output):

```
import_statements       — scan a folder for PDFs, parse and persist (idempotent)
generate_balance_sheet  — GAAP-style year-end balance sheet
generate_form_1065      — Form 1065 partnership return data
generate_k1             — Schedule K-1 per partner (partner_1 | partner_2)
pte_estimate            — quarterly estimated tax payments + due dates
reconcile_year          — inter-account transfer reconciliation
```

---

## MCP Integration (For AI Agents)

Any MCP-compatible client — Claude Desktop, Cursor, Wibey, Cline, Continue — connects
to your financial data via the spec-compliant MCP server:

```json
// ~/.claude/claude_desktop_config.json
{
  "mcpServers": {
    "ledger-agent": {
      "command": "ledger-agent-mcp"
    }
  }
}
```

Then ask Claude: *"What is my biggest expense category for 2024?"*
or *"Generate my Schedule K-1 for partner_1."*

**Privacy:** every MCP response passes through the R-46 PII firewall — account numbers,
EINs, SSNs, and partner names are replaced with opaque tokens before any data leaves the
host. Pass `_meta: { allow_pii: true }` to opt in to raw data.

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `import_statements` | Scan a folder for PDFs, parse and persist. Idempotent. |
| `generate_balance_sheet` | GAAP-style year-end balance sheet. |
| `generate_form_1065` | Form 1065 partnership return summary. |
| `generate_k1` | Schedule K-1 for `partner_1` or `partner_2` (configurable ownership split). |
| `pte_estimate` | Quarterly estimated tax payments with due dates. |
| `reconcile_year` | Inter-account transfer reconciliation. |

---

## AI Classification Pipeline

ledger-agent uses a **local-first AI design**:

```
Transaction description
        │
        ▼ Step 1: Learned rules (your past confirmations)     — free, instant
        ▼ Step 2: Local rule engine + rapidfuzz fuzzy match    — free, offline
        ▼ Step 3: OpenAI or Gemini (optional, low-confidence) — near-zero cost
        ▼ Step 4: Chart of Accounts keyword scan               — free
        ▼ Step 5: Interactive multiple-choice prompt           — remembered forever
```

The system **learns from you** — every classification you confirm is saved and
applied automatically next time. Over time, most transactions classify themselves.

### Privacy Note

When `FI_AI_BACKEND=openai` or `gemini`, transaction descriptions flow to the remote
API **only after** the R-46 firewall has tokenized sensitive values (account numbers,
partner names, routing numbers). Nothing leaves the machine in `local` mode (default).

---

## Supported Institutions

| Institution | Statement Type | Test Coverage |
|-------------|---------------|---------------|
| Bank X | Simple Business Checking | End-to-end |
| Broker Y | Brokerage / Investment Report | Detection |
| Bank X4 | Business Essentials Checking | Scaffolded |
| Bank X4 | Business Credit Card | Scaffolded |
| Bank X3 | Business Complete Checking | Scaffolded |
| Bank X2 | Business Checking | Scaffolded |
| Broker Z | Activity Statement (PDF) | Scaffolded |

> Institution names are pseudonymised in public source. Real detection tokens live
> in `private/institutions.py` (gitignored). See `private/institutions.example.py`
> for the template.

**Adding your bank:** create one parser file — `parsers/my_bank.py`. Decorate with
`@ParserRegistry.register`. The parser is auto-discovered at startup. Zero other edits required.

---

## Release Artifacts

Every merge to `main` auto-builds and publishes to GitHub Releases:

| Artifact | Form | Run with |
|----------|------|---------|
| `ledger-agent-core-vX.Y.Z.zip` | A | `pip install` |
| `ledger-agent-cli-vX.Y.Z.tar.gz` | B | `./run.sh` or `run.bat` |
| `ledger-agent-mcp-vX.Y.Z.zip` | C | `ledger-agent-mcp` |
| `ledger-agent-webapp-vX.Y.Z-linux-x86_64.jar` | D | `java -jar` |
| `ledger-agent-webapp-vX.Y.Z-darwin-aarch64.jar` | D | `java -jar` |
| `ledger-agent-webapp-vX.Y.Z-windows-x86_64.jar` | D | `java -jar` |
| `SHA256SUMS` | — | verify integrity |

---

## Why Open Source?

Small business owners are underserved by expensive, subscription-based accounting software.
Most QuickBooks alternatives still require cloud upload of sensitive financial data.
ledger-agent gives you:

- **Full data ownership** — nothing leaves your machine in local mode
- **Zero subscription** — free forever
- **AI-native** — MCP server connects to any AI assistant
- **Four surfaces, one engine** — CLI, MCP, webapp, or library
- **Auditable** — open source, no black box

---

## Comparison

| Feature | QuickBooks | Wave | **ledger-agent** |
|---------|-----------|------|-----------------|
| Price | $35–$235/mo | Free (upsell) | **Free forever** |
| Data ownership | Cloud only | Cloud only | **Local by default** |
| PDF import | Manual | Manual | **Automatic** |
| AI classification | Basic | None | **Local + optional GPT-4o-mini** |
| Form 1065 / K-1 | $235+/mo | No | **Built-in** |
| MCP / AI agent integration | None | None | **6 tools, privacy firewall** |
| Spring Boot webapp | No | No | **Self-contained fat jar** |
| Open source | No | No | **Yes** |

---

## For AI Agents Using This Repo

1. **Connect via MCP:** `pip install ledger-agent-mcp && ledger-agent-mcp` — six tools ready.
2. **Import statements:** call `import_statements` tool with folder path.
3. **Query data:** call `generate_balance_sheet` / `generate_form_1065` / `generate_k1`.
4. **Respect privacy:** responses are redacted by default; pass `allow_pii: true` only when needed.
5. **Four-form parity:** all forms produce identical numbers — you can verify with the CPA parity gate.

Full API: `ledger_agent/core/api.py`. Tool schemas: `ledger_agent/mcp/tools.py`. Architecture: `STRUCTURE.md`.

---

## Suggested GitHub Topics

```
accounting  small-business  bookkeeping  llc  sole-proprietor
balance-sheet  tax-estimator  pdf-parser  bank-statement  form-1065  schedule-k1
mcp  model-context-protocol  claude  ai-agent  spring-boot  fintech  python
```

---

## License

MIT — see [LICENSE](LICENSE).

> ledger-agent is not a substitute for professional accounting or tax advice.
> Always consult a qualified CPA for tax filings and a licensed financial advisor
> for investment decisions.

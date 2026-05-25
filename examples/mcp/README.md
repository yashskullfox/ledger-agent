# MCP Server Demo

This directory contains examples of using the `ledger-agent` MCP (Model Context
Protocol) server from an AI-agent perspective.

## Prerequisites

```bash
pip install -r requirements.txt
# The MCP server uses stdio transport — no port binding required
```

## Available tools (7)

| Tool | Description |
|------|-------------|
| `import_statements` | Import PDF bank/brokerage statements |
| `generate_balance_sheet` | Produce year-end balance sheet |
| `generate_form_1065` | Generate Form 1065 partnership return |
| `generate_k1` | Generate Schedule K-1 for a partner |
| `pte_estimate` | Estimate pass-through entity tax |
| `reconcile_year` | Match and reconcile inter-account transfers |
| `customer_summary` | Customer-readable outcome summary (W27) |

## Running the smoke test

```bash
python scripts/mcp_smoke.py
# Expected output: 7 tool names printed, exit code 0
```

## Example: balance sheet round-trip

```python
# See examples/mcp/demo_balance_sheet.py for a complete example
python examples/mcp/demo_balance_sheet.py
```

## Privacy

All MCP tool responses respect the PII firewall (`AGENTS.md §3`). Raw statement
text must not be passed to tool inputs without `allow_pii=True`.

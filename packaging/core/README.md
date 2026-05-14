# ledger-agent-core

Pure-Python financial intelligence library — zero CLI, UI, or network dependencies.

## Quick start (≤ 10 lines, copy-paste-runnable)

```bash
pip install ledger-agent-core
```

```python
from pathlib import Path
import ledger_agent.core.api as api

# Import all PDFs from a folder
report = api.import_statements(Path("~/statements").expanduser())
print(f"Imported {report.imported} statements")

# Build balance sheet for 2024
bs = api.generate_balance_sheet(2024)
print(f"Total assets: ${bs.total_assets:,.2f}")

# Quarterly tax estimate
est = api.pte_estimate(2024)
print(f"Quarterly payment: ${est.quarterly_payment:,.2f}")
```

## Six public functions

| Function | Description |
|---|---|
| `import_statements(folder)` | Scan folder for PDFs, parse and persist |
| `generate_balance_sheet(year)` | GAAP-style balance sheet |
| `generate_form_1065(year)` | Form 1065 partnership return data |
| `generate_k1(year, partner_id)` | Schedule K-1 partner share |
| `pte_estimate(year)` | Quarterly 1040-ES estimate |
| `reconcile_year(year)` | Inter-account transfer reconciliation |

## Supported institutions

Truist Bank, Chase, Bank of America, U.S. Bank (checking + credit card), Fidelity Brokerage, Interactive Brokers.

# 2024 CPA Parity Divergence — Diagnostic Note

**Lane**: W4-PARITY (research only — no production code touched)
**Date**: 2026-05-15
**Status**: Real divergence reproduced locally. Root cause hypothesis below is falsifiable.

## Symptom

`tests/integration/test_2024_cpa_parity.py::TestForm1065Parity::test_total_income` fails with:

```
PARITY FAILURE — Form 1065 — Total Income
  Computed:       28,601.00
  Reference:      28,101.00
  Divergence:        500.00  (tolerance: 1.00)
```

Cascading failures (same root cause — `total_income` flows into `ordinary_business_income`):
- `test_ordinary_business_income` (computed 18732+500 ≈ 19232 vs ref 18732)
- `test_total_deductions` (different cause — out of scope here, but ref 6369 vs unknown computed)
- `test_net_stcg` (also failing — out of scope)
- `test_p1_ordinary_income` (downstream of OBI)

The task prompt called this a "balance-sheet line"; the evidence shows it is actually a **Form 1065 P&L line — Gross Receipts / total_income**. The balance-sheet parity tests (`test_total_assets`, `test_total_equity`) pass.

## Suspect line item

**COA 4020 — Service Revenue.** Direct DB query on `data/db/financials.db`:

| coa_code | label | SUM(amount) 2024 | count |
|---|---|---|---|
| 4020 | Service Revenue | **28,601.00** | 3 |
| 4010 | Realised Trading Gains | 11,711.17 | 21 |
| 4021 | Dividend Income | 37.31 | 6 |

The three 4020 rows for 2024:

| date | description | amount |
|---|---|---|
| 2024-05-16 | `DEPOSIT` | **500.00** |
| 2024-05-24 | `DEPOSIT INTUIT14397315SYNCEDLLC CUSTOMERID...` | 101.00 |
| 2024-06-24 | `COUNTERDEPOSIT` | 28,000.00 |

CPA reference gross receipts = 28,101.00 = 101 + 28,000. The lone bare-`DEPOSIT` row (500.00) is the entire divergence.

## Hypothesis

The classifier mis-routes the bare `DEPOSIT` row into **4020 Service Revenue** because the COA seed in `ledger_agent/core/database.py:396` registers the keyword `"deposit"` against 4020:

```python
("4020", "Service Revenue", "revenue", "4000", "", '["intuit","deposit","invoice"]'),
```

The keyword matcher in `ledger_agent/core/intelligence/classifier.py:44-59` (`_keyword_match`) does a case-insensitive substring scan. Description `"DEPOSIT"` contains `"deposit"` → single match → auto-classified 4020. The CPA evidently treated this $500 as a non-revenue inflow (capital contribution, intra-bank xfer, or owner draw reversal) and excluded it from line 1.

This hypothesis is consistent with:
- Exact 500.00 delta (one txn, no rounding noise).
- Only the bare `DEPOSIT` lacks the disambiguating `INTUIT` token that the more specific `("DEPOSIT INTUIT", "4020", ...)` seed in `memory.py:163` targets.
- `"deposit"` is also listed under COA 1000 keywords (`database.py:369`) but that's an asset code, not a revenue code, so the matcher's "single best entry" rule still picks 4020 over 1000 only because revenue/expense codes are the ones scanned for P&L. [unverified — would need to confirm exact COA pool passed to `_keyword_match` for this txn]

## Verification step

Run from `/Users/vn53fda/Downloads/TinyProject/ledger-agent/`:

```bash
.venv/bin/python -c "
import sqlite3
con = sqlite3.connect('data/db/financials.db')
cur = con.cursor()
cur.execute(\"SELECT date, description, amount, coa_code, coa_name, classifier_version FROM transactions WHERE statement_period LIKE '2024-%' AND amount='500.00' AND description='DEPOSIT'\")
for r in cur.fetchall(): print(r)
"
```

Expected (confirms hypothesis): one row, `coa_code='4020'`, `coa_name='Service Revenue'`.
If `coa_code` is anything else (e.g. `9999`, `9000`, `3010`), the hypothesis is refuted.

Secondary check — is the keyword `"deposit"` the cause:

```bash
.venv/bin/python -c "
from ledger_agent.core.intelligence.classifier import _keyword_match
from ledger_agent.core.models import COAEntry
entries = [COAEntry('4020','Service Revenue','revenue','4000','',['intuit','deposit','invoice'])]
print(_keyword_match('DEPOSIT', entries))
"
```

Expected: returns the 4020 entry (confirms keyword fires on bare `DEPOSIT`).

## Fix sketch

Two narrow change-sites, neither touched here:

1. **`ledger_agent/core/database.py`** — COA seed list, line 396: remove `"deposit"` from the 4020 keyword array (leave `"intuit","invoice"`). Rationale: `"deposit"` is too generic to be a unique revenue signal; the specific `"DEPOSIT INTUIT"` memory rule in `memory.py:163` already handles the genuine revenue case.

2. **`ledger_agent/core/intelligence/classifier.py`** — function `classify_transaction`: when description is exactly `"DEPOSIT"` (or any short generic banking token), route to `UNCLASSIFIED_CODE='9999'` for manual review rather than guessing. Optional belt-and-braces.

Either change alone should drop `total_income` from 28,601 → 28,101 and bring the parity gate green. Re-run `scripts/regen_parity_corpus.py` afterwards is **not** needed — the fixture is correct; the production classifier is wrong.

## Not investigated (out of scope for this lane)

- `test_total_deductions` and `test_net_stcg` are also failing. Both are independent line items and need their own diagnostic passes. [unverified — could share a root cause via COA-keyword over-matching, but no evidence collected here.]

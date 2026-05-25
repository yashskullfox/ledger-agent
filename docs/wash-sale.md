# Wash-Sale Disallowance (W16)

**Ticket**: W16-WASH-SALE  
**Status**: Framework implemented; requires `private/wash_sale_adjustments.csv` for local accuracy  
**Reference**: IRC §1091; Form 1065 Schedule K line 8

---

## What is a wash-sale disallowance?

A wash sale occurs when a security sold at a loss is repurchased within 30 days before or after # redaction: allow
the sale date. IRC §1091 disallows the loss for that tax year. The disallowed amount is reported
in box 1g of the 1099-B and added back to the net short-term capital gain figure on Schedule K.

## How the engine handles it

The engine cannot read wash-sale columns from broker PDFs directly (they appear only on the
1099-B, which is a private gitignored document). Instead, the engine loads a CSV of total
disallowances from `private/wash_sale_adjustments.csv`:

```csv
ticker,disallowed_loss
TICKER_SEC1,0.00
TICKER_SEC2,0.00
```

If the CSV is absent, the engine computes raw net STCG without the adjustment and logs a warning.
The `test_net_stcg` parity test remains `xfail` in public CI (no private CSV present).

## Setup (local development)

1. Obtain the 1099-B for the fiscal year (located in `statements/<year>/` — gitignored).
2. Copy `private/wash_sale_adjustments.example.csv` → `private/wash_sale_adjustments.csv`.
3. Fill in the `disallowed_loss` column for each ticker from box 1g of the 1099-B.
4. Run `pytest tests/unit/test_wash_sale.py -v` to verify the CSV parses correctly.
5. Run `pytest -m parity tests/integration/test_2024_cpa_parity.py::TestForm1065Parity::test_net_stcg`
   to verify the adjustment produces the correct CPA-reference STCG.

## Environment variable override

Set `FI_WASH_SALE_CSV=/path/to/adjustments.csv` to point the engine at a non-default path.

## Technical details

- Module: `ledger_agent/core/accounting/wash_sale.py`
- Function: `total_disallowed(csv_path=None) → Decimal`
- Called by: `generate_form_1065()` in `ledger_agent/core/api.py`
- Adjustment is added to `net_short_term_capital_gain` (positive = disallowed loss added back)

# CLI Usage Examples

`ledger-agent` ships four forms from one core (R-50).  
This page shows the **CLI (Form B)** invocations and their human-readable output.

---

## Quick-start

```bash
# 1. Import statements from a folder of PDF statements
ledger scan ~/statements/2024/

# 2. View customer outcome summary (recommended first command)
ledger summary 2024

# 3. Generate a balance sheet
ledger balance 2024

# 4. Generate Form 1065
ledger form1065 2024

# 5. Generate K-1s for each partner
ledger k1 2024

# 6. Estimate quarterly PTE payments
ledger tax 2024

# 7. Run year-end reconciliation
ledger reconcile 2024
```

---

## `ledger summary <year>` — Customer outcome summary (W28)

Single command that answers the four business questions without reading raw JSON:

```
$ ledger summary 2024

Customer Outcome Summary — 2024
  Period covered:       2024-01 to 2024-12
  P/L status:           PROFIT  (OBI:      +7,400.00)
  Growth signal:        STABLE — Revenue on track with prior period.

Balance Sheet (2024-12):
  Total Assets:             50,000.00
  Total Liabilities:             0.00
  Total Equity:             50,000.00
  Health:               HEALTHY

PTE / Tax:
  PTE status:           NOT_DUE  (annual est: 740.00)

Confidence:
  CLOSE_READY

Next actions:
  • File Form 1065 for fiscal year 2024.
```

### With `--no-prompt` for scripting / AI agents

Returns the W27 contract as JSON:

```bash
ledger summary 2024 --no-prompt
```

```json
{
  "fiscal_year": 2024,
  "period_covered": "2024-01 to 2024-12",
  "profit_or_loss": {
    "status": "profit",
    "signal": "positive",
    "ordinary_business_income": 7400.00
  },
  "growth_signal": {
    "status": "stable",
    "basis": "prior_year",
    "note": "Revenue on track with prior period."
  },
  "balance_sheet_health": {
    "is_balanced": true,
    "status": "healthy",
    "total_assets": 50000.00,
    "total_liabilities": 0.00,
    "total_equity": 50000.00,
    "period": "2024-12",
    "skipped_accounts": 0
  },
  "pte_due_signal": {
    "status": "not_due",
    "annual_estimate": 740.00,
    "next_due": "Q1 2025"
  },
  "tax_due_signal": {
    "status": "monitor",
    "basis": "pte_estimate",
    "note": "Review quarterly."
  },
  "confidence_flags": ["CLOSE_READY"],
  "next_actions": [
    "File Form 1065 for fiscal year 2024."
  ]
}
```

### Confidence flags

| Flag | Meaning |
|---|---|
| `CLOSE_READY` | All correctness gates pass — ready for CPA review |
| `NOT_CLOSE_READY_W15` | COGS classification not yet verified |
| `NOT_CLOSE_READY_W16` | Wash-sale adjustments not yet applied |
| `NOT_CLOSE_READY_W17` | One or more account snapshots are missing |
| `BLOCKED_NO_DATA` | No statement data found for the requested year |

---

## `ledger balance <year>` — Balance sheet

```
$ ledger balance 2024

Balance Sheet — 2024-12
  Entity:      ENTITY_A

  ASSETS
    1010  Cash — BANK_X Checking     ~$XX,XXX
    1020  Cash — BANK_X4 Checking    ~$XX,XXX
    1030  Brokerage Portfolio         ~$XX,XXX
  ─────────────────────────────────────────────
  TOTAL ASSETS                       ~$XX,XXX

  LIABILITIES                             0.00
  EQUITY                             ~$XX,XXX

  Balanced: ✅  Coverage: 4/4 accounts consumed
```

---

## `ledger form1065 <year>` — Form 1065 summary

```
$ ledger form1065 2024

Form 1065 — Fiscal Year 2024
  Total Income:          ~$XX,XXX
  Cost of Goods Sold:     ~$X,XXX
  Gross Profit:          ~$XX,XXX
  Total Deductions:       ~$X,XXX
  Ordinary Business Income: ~$X,XXX
  Investment Interest:        0.00
  Net STCG:                   0.00
```

---

## `ledger k1 <year>` — K-1s for all partners

```
$ ledger k1 2024

K-1 — partner_1 (99.0%)
  Ordinary Income/Loss:   ~$X,XXX
  Net STCG:                   0.00
  Dividends:                  0.00
  Interest:                   0.00

K-1 — partner_2 (1.0%)
  Ordinary Income/Loss:      ~$XX
  Net STCG:                   0.00
```

---

## `ledger tax <year>` — PTE quarterly estimates

```
$ ledger tax 2024

PTE Estimate — 2024
  Annual estimate:   ~$XXX
  Q1 due:            ~$XXX  (Apr 15)
  Q2 due:            ~$XXX  (Jun 15)
  Q3 due:            ~$XXX  (Sep 15)
  Q4 due:            ~$XXX  (Jan 15)
```

---

## `ledger reconcile <year>` — Year-end reconciliation

```
$ ledger reconcile 2024

Reconciliation 2024: CLEAN
  Matched transfers:   4
  Unmatched:           0
  Total transfer flow: ~$XX,XXX
```

---

## Notes

- All amounts above use `~$X,XXX` notation per the privacy policy
  (`config/redaction_corpus.yaml`). Real amounts appear only in your local
  `data/db/financials.db` (gitignored).
- Add `--no-prompt` to any command to receive JSON output suitable for piping
  or AI-agent consumption.
- Full flag reference: `ledger --help`

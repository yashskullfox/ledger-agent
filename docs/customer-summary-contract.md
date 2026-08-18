# Customer Outcome Summary Contract

**Status**: APPROVED for implementation (W27)
**Last updated**: 2026-05-26  
**Approved by**: Consultant team (ARCHITECT/PRODUCT/PLANNER)  
**Purpose**: Provide one consistent, customer-readable outcome shape across Form B (CLI), Form C (MCP), and Form D (Webapp).  
**Implementation wave**: 2 (W28/W29/W30 — parallel CLI + Webapp + MCP, ~2 weeks)  
**Depends on**: W26 (test regression fix)

---

## 1) Contract goals

This contract exists so a customer can answer, from one output:

1. Are we currently in profit or loss for the fiscal year?
2. Is our balance sheet healthy?
3. Is PTE/tax likely due now?
4. How confident is this result given data/runtime quality?
5. What should we do next?

---

## 2) Canonical JSON shape

```json
{
  "fiscal_year": 2026,
  "period_covered": "YYYY-MM..YYYY-MM",
  "profit_or_loss": {
    "status": "profit|loss|break_even",
    "signal": "positive|negative|neutral",
    "ordinary_business_income": "decimal-string"
  },
  "growth_signal": {
    "status": "growing|flat|contracting|unknown",
    "basis": "equity|assets|net_income",
    "note": "short human-readable explanation"
  },
  "balance_sheet_health": {
    "is_balanced": true,
    "status": "healthy|review_needed|unknown",
    "total_assets": "decimal-string",
    "total_liabilities": "decimal-string",
    "total_equity": "decimal-string"
  },
  "pte_due_signal": {
    "status": "likely_due|not_due|unknown",
    "annual_estimate": "decimal-string",
    "next_due": "string"
  },
  "tax_due_signal": {
    "status": "likely_due|not_due|unknown",
    "basis": "form_1065|pte_estimate",
    "note": "short human-readable explanation"
  },
  "confidence_flags": [
    "string"
  ],
  "next_actions": [
    "string"
  ]
}
```

---

## 3) Confidence flag policy

Always include at least one confidence flag. Suggested flags:

- `CLOSE_READY` — all hard gates passed.
- `NOT_CLOSE_READY_W15` — COGS structural gap unresolved.
- `NOT_CLOSE_READY_W16` — wash-sale adjustment unresolved.
- `NOT_CLOSE_READY_W17` — snapshot completeness gap unresolved.
- `NOT_CLOSE_READY_W26` — regression/failing integration tests unresolved.
- `BLOCKED_ENV_DEPENDENCY` — runtime dependency missing (example: parser dependency absent).
- `BLOCKED_PRIVATE_CONFIG` — `private/institutions.py` or required private inputs missing.

If any `NOT_CLOSE_READY_*` or `BLOCKED_*` flag is present, surfaces must show a plain-language caution before numeric details.

---

## 4) Rendering guidance by surface

- **CLI (Form B)**: print 6-10 lines, top-down, with a final `Confidence:` line and `Next actions:` list.
- **MCP (Form C)**: return JSON strictly following this schema; no prose-only responses.
- **Webapp (Form D)**: show summary cards first; raw JSON in a collapsed "Technical details" section.

---

## 5) Non-goals

- This contract does not replace Form 1065, K-1, or CPA review.
- This contract does not expose raw private statement text.
- This contract does not bypass redaction/privacy gates.

---

## 7) Implementation guidance by form

### Form B — CLI (`ledger_agent/cli/main.py`)
- Command: `ledger summarize <fiscal_year>`
- Output: 10-15 lines of plain language, Rich-formatted tables and panels
- Confidence caution must appear **before** numeric details if any `NOT_CLOSE_READY_*` flag is present
- Raw JSON optional via `--json` flag

### Form C — MCP (`ledger_agent/mcp/tools.py`)
- Tool name: `customer_summary`
- Input: `{"fiscal_year": 2024}`
- Output: JSON strictly matching this schema
- Confidence flags must be present; AI agents use them to calibrate trust in the result

### Form D — Webapp (`webapp/src/main/resources/templates/results.html`)
- Display mode: Summary cards first (profit/loss, balance sheet, PTE, tax, confidence)
- Raw JSON remains available in a collapsed "Technical details" section below
- Confidence caution appears as a warning panel if any gate is not close-ready

---

## 8) Approval & sign-off

**Approved**: 2026-05-25 by Consultant team  

| Role | Verdict | Evidence |
|---|---|---|
| ARCHITECT | ✅ Approve | Contract aligns with R-50 single-source-of-truth principle; same shape across all forms prevents divergence. |
| PRODUCT | ✅ Approve | Addresses customer intent (profit/loss, growth, PTE, tax, balance-sheet health) in one readable output. |
| PLANNER | ✅ Approve | Scope is defined; W28/W29/W30 lanes are parallel and non-blocking. ~2 weeks to complete. |

**Gate status for downstream lanes**:
- W28 (CLI): READY TO DISPATCH
- W29 (Webapp): READY TO DISPATCH  
- W30 (MCP): READY TO DISPATCH
- All three can run in parallel after W26 (test fix) is merged.

**Privacy**: Contract does not require changes to privacy/audit infrastructure. No new PII vectors. Scanner remains clean.

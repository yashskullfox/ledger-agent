> **Privacy notice.** This document uses the pseudonym scheme defined in `config/redaction_corpus.yaml`. Real entity names, partner names, bank names, broker names, tickers, and account numbers are **not** present in this file. See the policy below for the full scheme.

# Doc-Redaction Policy (ARCH-33)

## Purpose

The `ledger-agent` repo is public. Committed artefacts — design docs, test fixtures, YAML configs, SQL samples, and this file — must never contain real identifiers for the partnership under test, its partners, its financial institutions, or its held securities.

This policy describes the pseudonym scheme, the enforcement mechanism, and how to author scanner-clean content.

---

## Pseudonym scheme

All real identifiers are replaced with pseudonyms from `config/redaction_corpus.yaml`. The scanner (`scripts/check_doc_redaction.py`) treats `replacement_tokens` entries as authoritative safe values — they are never flagged.

| Category | Pseudonym | Examples of what it replaces |
|---|---|---|
| Partnership entity | `ENTITY_A` | LLC legal name, trade name, DBA |
| Second entity | `ENTITY_B` | Any other legal entity appearing in fixtures |
| Majority partner | `PARTNER_1` | First name, last name, full name |
| Minority partner | `PARTNER_2` | First name, last name, full name |
| Checking bank | `BANK_X` | Bank name, routing-number-owner |
| Primary broker | `BROKER_Y` | Brokerage firm name |
| Secondary broker | `BROKER_Z` | Second brokerage firm name |
| Equity security 1 | `TICKER_SEC1` | Ticker symbol, company name |
| Equity security 2 | `TICKER_SEC2` | Ticker symbol, company name |
| Account number | `acct_****NNNN` | Full account number masked to last-4 |
| Small dollar figure | `~$X,XXX` | Cent-precision figure in $1k–$9.9k range |
| Medium dollar figure | `~$XX,XXX` | Cent-precision figure in $10k–$99k range |
| Large dollar figure | `~$XXX,XXX` | Cent-precision figure in $100k–$999k range |
| Generic financial | `[REDACTED:cash]` | Any other cent-precision figure near a financial noun |
| Capital figure | `[REDACTED:capital]` | Partner capital-account balances |

---

## What triggers a scanner hit

The scanner flags content in one of three ways:

1. **Literal token match** — a real name from the corpus appears verbatim. The scanner never prints the matched value; it prints only `path:line:col: hit <category>`.

2. **Account-number regex** — 8–17 consecutive digits appear in committed text. Last-4-only patterns (`****1234`) are excluded.

3. **Financial-figure proximity** — a cent-precision dollar amount appears within 5 tokens of a financial noun (balance, cash, equity, capital, dividend, proceeds, income, revenue, profit, loss, deduction, assets, liabilities, etc.).

---

## How to author scanner-clean content

### DO
- Use pseudonyms from `replacement_tokens` in `config/redaction_corpus.yaml`.
- Write relative magnitudes: "roughly one order of magnitude larger than `PARTNER_2`".
- Use `~$X,XXX` / `~$XX,XXX` / `~$XXX,XXX` for approximate figures.
- Write account numbers as `acct_****1234` (last-4 only).

### DO NOT
- Paste real entity/partner/bank/broker/ticker names.
- Include cent-precision dollar figures near financial nouns (`$38,204.61` near "balance").
- Include full account numbers or routing numbers.
- Include statement file paths that reveal institution names.

### Allow-list for known exceptions
Append `  # redaction: allow` to any line the scanner should ignore:
```
# This file is for ENTITY_A (the partnership)  # redaction: allow
```

Add entire paths to `redaction.allowlist` (one per line, relative to repo root):
```
tests/fixtures/known_good_output.txt
```

---

## Enforcement

| Gate | Trigger | Blocks |
|---|---|---|
| `scripts/check_doc_redaction.py` | Manual / `make check-docs` | Developer workflow |
| `hooks/pre-commit` | `git commit` | Local commit |
| `.github/workflows/doc-redaction.yml` | Pull request | Merge to main |

Install the pre-commit hook:
```bash
make install-hooks
```

Run the scanner manually:
```bash
# Staged files only (fastest, for use in hooks):
python scripts/check_doc_redaction.py --staged

# All tracked files:
python scripts/check_doc_redaction.py --all-tracked

# Specific paths:
python scripts/check_doc_redaction.py --paths docs/ config/
```

Exit code `0` = clean. Exit code `1` = hits found. Hits are printed as:
```
docs/design.md:42:7: hit entity (ENTITY_A)
```

The matched value is **never** printed.

---

## Mapping real names to pseudonyms (local only)

Maintain a local-only mapping file at `private/pseudonym-map.local.md` (gitignored under R-78):

```markdown
# private/pseudonym-map.local.md  —  NEVER COMMIT THIS FILE
| Pseudonym | Real value |
|---|---|
| ENTITY_A | ... |
| PARTNER_1 | ... |
```

This file is the authoritative source for populating `entities`, `partners`, `banks`, `brokers`, and `tickers` in `config/redaction_corpus.yaml` with real values for local development.

---

## Rationale

The repo remote is public. A single `git add` of a file containing real identifiers makes that information permanently public via git history. The cost of pseudonymisation is low; the cost of a leak is unbounded. This policy front-loads the cost.

See §3.3 of `requirement-and-review-feedback.md` for the triggering finding (DOC-1) and ARCH-33..38 for the full implementation plan.

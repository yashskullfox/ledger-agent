# AGENTS.md — ledger-agent
> Pseudonyms only in tracked files: `ENTITY_A`, `PARTNER_1/2`, `BANK_X/X2/X3/X4`, `BROKER_Y/Z`, `TICKER_SEC1/2`, `acct_****1234`.  
> Real values live in `data/`, `statements/`, `private/` (all gitignored).  
> **Last reviewed 2026-05-25.** Full work history → `requirement-and-review-feedback.md`. Consultant onboarding → `docs/REQUIREMENTS.md`.

---

## 1. Three rules (read before touching anything)

1. **Never mark ✅ without on-disk evidence.** `grep`/`ls`/run the test. The 2026-05-14 hardening pass re-opened ~19 fabricated rows — don't repeat this.
2. **Privacy contract (R-73/R-74) overrides everything.** No real entity, partner, bank, ticker, account number, or cent-precise figure in any tracked artefact. Use `config/redaction_corpus.yaml`. Run `python scripts/check_doc_redaction.py --staged` before every commit.
3. **PII firewall fails closed.** Raw PII must not leave the host without `allow_pii=True`. Accidental egress to a model provider, stdout, or disk outside `data/` is a P0 incident.

---

## 2. Project overview

Partnership accounting tool for a US LLC filing Form 1065. Reads PDF statements → classifies transactions → produces balance sheets, K-1s, Form 1065, PTE tax estimates. Ships as **four forms** from one core (R-50):

| Form | Entry point |
|---|---|
| A — Python library | `import ledger_agent.core.api` |
| B — CLI | `./run.sh balance 2025` |
| C — MCP server | `ledger-agent-mcp` |
| D — Spring Boot jar | `java -jar ledger-agent-webapp-*.jar` |

All four forms must produce identical numbers. Divergence > $1 is P0 (R-51 / ARCH-32).

---

## 3. Setup & test

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                               # 344+ passed, 0 failed
python scripts/check_doc_redaction.py --strict --all-tracked  # must stay at 0 hits
```

`private/institutions.py` (gitignored) must be populated from `private/institutions.example.py` for parsers to detect real PDFs. Without it, all 7 parsers return `can_parse=False`.

---

## 4. Repository layout (verified 2026-05-25)

```
ledger_agent/core/
  api.py                ← 6 public functions (single source of truth)
  database.py           ← SQLite; SCHEMA_VERSION=6
  models.py             ← Entity/Account/Transaction/Position/Snapshot/PositionType
  parsers/              ← bank_x{,2,3,4}_checking, bank_x4_creditcard,
                           broker_y_brokerage, broker_z  (+base, registry)
  accounting/           ← balance_sheet, continuity, tax_estimator
  intelligence/         ← classifier.py (CLASSIFIER_VERSION="1.0")
  reports/renderer.py   ← export_balance_sheet_json (writes *.coverage.json)
  privacy.py / audit.py / cleanup.py
ledger_agent/cli/main.py · mcp/ · bridge/jsonrpc_stdio.py
tests/
  integration/          ← test_2024_cpa_parity, test_aggregation_no_silent_drop,
                           test_classification_persisted, test_liability_recognition,
                           parsers/test_position_completeness
  unit/test_continuity.py
  architecture/test_core_purity.py
scripts/check_doc_redaction.py · regen_parity_corpus.py
config/redaction_corpus.yaml
docs/REQUIREMENTS.md · docs/developer-tasks.md
private/institutions.example.py  (real tokens gitignored)
statements/2026/                 ← 8 USB PDFs (checking + credit card, Jan–May 2026)
```

`STRUCTURE.md` references a top-level `core/` — it no longer exists; everything is under `ledger_agent/core/`. The on-disk view wins.

---

## 5. Current state (2026-05-25)

| Ticket | Status | Key files |
|---|---|---|
| ARCH-25 PositionType + parsers | ✅ Done | `models.py`, `broker_y_brokerage.py`, `broker_z.py`, `test_position_completeness.py` |
| ARCH-26 Coverage manifest | ✅ Done | `balance_sheet.py:138-173`, `renderer.py`, `test_aggregation_no_silent_drop.py` |
| ARCH-27 Persist classification | ✅ Done | `classifier.py`, `database.py` (`update_coa_with_meta`), `test_classification_persisted.py` |
| ARCH-28 Margin liability | ✅ Done | `balance_sheet.py:268-281`, `test_liability_recognition.py` |
| W26 Test regression (coa_code) | ✅ Done | `test_liability_recognition.py` lines 122, 161 |
| W27 Customer summary contract | ✅ Done | `docs/customer-summary-contract.md` (APPROVED); `core/api.py` `CustomerSummary` dataclass |
| W28 CLI summary command | ✅ Done | `ledger_agent/cli/main.py` `cmd_summary()` — `ledger summary <year>` |
| W29 Webapp result cards | ✅ Done | `webapp/src/main/resources/templates/results.html` — summary cards + collapsed raw JSON |
| W30 MCP customer_summary tool | ✅ Done | `ledger_agent/mcp/tools.py` — 7th tool; `tests/integration/test_mcp_privacy.py` updated |
| bank_x4_checking "Customer Deposits" | ✅ Done | `bank_x4_checking.py` `_parse_customer_deposits()`; `test_bank_x4_2026.py` |
| ARCH-29..32 | ⛔ Blocked | Spec (req doc §4) missing from disk — owner must supply |
| W15-COGS-LINE | ⚠️ xfail | 3 parity tests need COGS structural fix in `api.py` — owner spec required |
| W16-WASH-SALE | ⚠️ xfail | `private/wash_sale_adjustments.csv` gitignored; xfail in CI — owner must supply CSV |
| W17-DATA-REFRESH | ⚠️ skip | Missing 2024 `account_snapshots`; BANK_X2/X3 detection tokens unconfigured — owner must supply |
| W15..W25 open-source prep | PENDING | See `requirement-and-review-feedback.md` Phase 7 |

---

## 6. Coding rules

- **Core purity (ARCH-02):** `ledger_agent/core/` must NOT import `cli`, `rich`, `click`, `typer`, `requests`, `httpx`, `fastapi`, `flask`, `questionary`, `colorama`. Verified by `test_core_purity.py`.
- **Single source of truth:** All six core operations live in `ledger_agent.core.api`. Forms B/C/D are thin wrappers — no reimplementation.
- **Schema changes:** bump `SCHEMA_VERSION` in `database.py` and add a migration block in `init_db()`. Never rename existing columns.
- **Parser registration:** subclass `BaseStatementParser`, decorate with `@ParserRegistry.register`. Detection tokens from `private/institutions.py` only.
- **Classification:** call `TransactionRepo.update_coa_with_meta()` at classify time. Never classify at report time.
- **Coverage:** every account must appear in `consumed_snapshots` or `skipped_snapshots`. Silent drops are forbidden.
- **Ruff** (`pyproject.toml`) — run before committing. Target `py310`, line-length 100.

---

## 7. Commit rules

- Conventional commits only (`feat:`, `fix:`, `refactor:`, `docs:` etc.).  
- Stage explicit paths — never `git add -A`.  
- Never `--no-verify`. Fix the scanner hit; add `# redaction: allow` only for genuine false positives.  
- Forbidden in any commit: real names, cent-precise figures near financial nouns, account numbers, anything under `data/`, `statements/`, `private/`, `*.pdf`, `*.db`, `*.xlsx`, `*.csv`.

---

## 8. Forbidden patterns (grounds for rejection)

1. ✅ flip without on-disk verification (`grep`/`ls`/test).
2. Real name / figure / account number in any tracked file.
3. Raw PII read outside `allow_pii=True` opt-in.
4. Network egress (`requests`, `httpx`) anywhere in `ledger_agent/core/`.
5. Core-purity violation (see §6).
6. Reimplementing core logic in B/C/D wrappers.
7. Agent code reading `private/pseudonym-map.local.md`.
8. Missing audit log on any tool-call exit path.
9. Bare `except:` or `except BaseException` in privacy/audit/cleanup.
10. Echoing matched values in scanner/error output — pattern is `path:line:category` only.

---

## 9. Quick reference

| Need | File |
|---|---|
| Developer task cards & sprint board | `docs/developer-tasks.md` |
| Ticket status board | `requirement-and-review-feedback.md` |
| Consultant onboarding | `docs/REQUIREMENTS.md` |
| Customer output contract | `docs/customer-summary-contract.md` |
| Pseudonym corpus | `config/redaction_corpus.yaml` |
| Privacy/redaction policy | `docs/redaction-policy.md` |
| Parity diagnostic | `docs/parity-divergence.md` |
| Schema version | `ledger_agent/core/database.py` `SCHEMA_VERSION` |
| Core API | `ledger_agent/core/api.py` (7 functions: import_statements, generate_balance_sheet, generate_form_1065, generate_k1, pte_estimate, reconcile_year, build_customer_summary) |
| MCP tool schemas | `ledger_agent/mcp/tools.py` `TOOL_SCHEMAS` (7 tools) |
| Audit log | `ledger_agent/data/audit/run-<run_id>.jsonl` |

*When in doubt, prefer honesty about what is and is not on disk.*

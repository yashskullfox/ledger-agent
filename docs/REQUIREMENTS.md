# Ledger Agent — Consultant Requirements & Onboarding
**As of**: 2026-05-25  
**Audience**: New developer / consultant agent picking up this project  
**Primary refs**: `AGENTS.md` (architecture contract) · `requirement-and-review-feedback.md` (full work history) · `docs/developer-tasks.md` (tasks) · `docs/customer-summary-contract.md` (output contract)

---

## QUICK START (15 minutes)

**New here?** Follow this:

1. **Read the three rules** (AGENTS.md § 1, 5 min):
   - Never mark done without evidence (run tests, grep files)
   - Privacy contract overrides everything (run scanner before commit)
   - PII firewall fails closed (no PII outside gitignored folders)

2. **Understand the goal** (This section, 5 min):
   - Customer wants: profit/loss, growth, PTE due, tax due, balance-sheet health **in one readable output**
   - Currently: endpoints exist but outputs are raw JSON, not customer-friendly
   - Timeline: 2.5 weeks (W26–W30) for readable outputs + 3 weeks (W15–W17) for accounting correctness

3. **Pick a task** (docs/developer-tasks.md, 5 min):
   - **W26** (15 min): Fix test regression → unblocks everything
   - **W27** (30 min): Approve customer contract → gates output layer
   - **W28** (7h): CLI `summarize` command OR
   - **W29** (7h): Webapp summary cards OR
   - **W30** (4h): MCP summary tool

**First commit checklist**:
- [ ] Tests pass: `pytest -q`
- [ ] Scanner clean: `python scripts/check_doc_redaction.py --strict --all-tracked` (0 hits)
- [ ] Conventional commit: `fix: ...` or `feat: ...` or `docs: ...`
- [ ] No `--no-verify` (scanner is not optional)

---

## 1. What This Project Is

`ledger-agent` is a **partnership accounting tool** for a two-partner LLC (`ENTITY_A`, `PARTNER_1`, `PARTNER_2`). It reads bank and brokerage PDF statements, classifies transactions against a Chart of Accounts, and produces:

- Balance sheets (assets, liabilities, members' equity)
- Form 1065 (Partnership return)
- Schedule K-1 per partner
- Quarterly PTE tax estimates
- Reconciliation reports

It ships as **four forms** from one Python core (`ledger_agent.core`):

| Form | Entry point |
|---|---|
| A — Python library | `import ledger_agent.core.api` |
| B — CLI | `./run.sh balance 2025` |
| C — MCP server (AI-agent) | `ledger-agent-mcp` |
| D — Spring Boot webapp | `java -jar ledger-agent-webapp-*.jar` |

All four forms must produce identical numbers (`R-51 / ARCH-32`).

---

## 1.5 Known Limitations & Scope Boundaries

This is a **specialized tool for a narrow use case**, not a general accounting platform. Understand these boundaries before investing:

### 1. Target Audience is Small
- **Entity type**: US Form 1065 partnerships (2 partners) only
- **Excludes**: S-corps, C-corps, sole proprietors, international entities, multi-member LLCs
- **Use case**: Partnership accounting + tax preparation, not personal finance, budgeting, or investment analysis
- **Status**: Early-stage (12 days old as of 2026-05-25); zero production deployments known

### 2. Financial Institution Coverage is Limited
- **Supported**: 7 specific institutions (3 banks: BANK_X, BANK_X2, BANK_X3, BANK_X4; 2 brokers: BROKER_Y, BROKER_Z)
- **Not supported**: Fidelity, IBKR, most regional banks (parsers don't exist; would require custom Python implementation)
- **2026 data**: USB checking + credit card 100% covered; Fidelity/IBKR ~11 PDFs not parsed yet (parser count: 2/7 only)
- **Adding new institutions**: Requires writing a custom parser class (not no-code; requires Python proficiency)

### 3. Incomplete Specification (Active Blockers)
- **ARCH-29..32**: Four architecture tickets blocked on owner supplying spec documents (not on disk as of 2026-05-25)
- **W15-COGS-LINE**: Structural accounting fix marked xfail (3 tests) — needs owner approval before implementation
- **W16-WASH-SALE**: Wash-sale disallowance marked xfail — owner must supply private CSV spec
- **Implication**: Core accounting features are gated on external sign-off; not self-sufficient in current state

### 4. Onboarding Friction
- **Private config required**: `private/institutions.py` must be populated with real bank credentials before any parser detects PDFs
- **Parser activation**: All 7 parsers silently degrade if detection tokens are missing (elegant, but not obvious to new users)
- **Special parsing cases**: "Customer Deposits" ACH section in BANK_X4 required custom regex (W30 task); other banks likely have similar hidden cases
- **CI/CD implications**: public CI can't test full pipeline without private credentials (acceptable for open source, but limits validation)

### 5. Single-User, Pre-Production Project
- **Age**: 12 days old (initial commit ~2026-05-13)
- **Community**: Zero stars, zero external contributors, zero production deployments known
- **Concurrency**: No multi-user testing; assumes single operator
- **Stability**: Consultant team identified 0 active P0 bugs; readability layer is implemented, but close-readiness gates remain

### 6. CI/CD & Release Pipeline Validation Status
- **Release workflow**: `.github/workflows/release.yml` is present on disk (10-job pipeline)
- **CPA parity tests**: Require `FI_CPA_CORPUS_PATH=statements/2024.txt` corpus; not in public visibility (gitignored or missing)
- **Deploy pipeline**: End-to-end dry-run evidence still required in project docs (`docs/ci-dry-run-evidence.md`)
- **Implication**: "Ready for production" requires W22 dry-run evidence + W5 history squash + W18 release gate

### What This Means For You

| If you want... | This project is... | Because... |
|---|---|---|
| **Multi-entity consolidation** | Not a fit | Single LLC only; no rollup logic |
| **Support for your bank** | Risky | Need custom parser; cost/benefit trade-off |
| **Production deployment in 2 weeks** | Not realistic | W15–W17 (accounting) + W22 (release dry-run) needed; 3+ weeks minimum |
| **Accounting correctness guarantee** | Premature | W15/W16 xfails still open; CPA parity not 100% |
| **Personal finance feature** | Wrong tool | This is tax prep only; no budgeting, forecasting, or analytics |
| **Automated bank connection** | Not implemented | Manual PDF uploads only; Plaid-style APIs not in scope |

---

## 2. Environment Setup

```bash
# Python 3.10–3.12 required
pip install -r requirements.txt -r requirements-dev.txt

# Create private/institutions.py from template (gitignored — holds real bank tokens)
cp private/institutions.example.py private/institutions.py
# → fill in real detection strings for each bank/broker

# Run the test suite
pytest -q                                    # should be 344+ passed, 0 failed

# Run the redaction scanner (must stay at 0 hits)
python scripts/check_doc_redaction.py --strict --all-tracked
```

**Critical gitignored files** (absent in CI and fresh clones — parsers degrade gracefully):

| File | Purpose |
|---|---|
| `private/institutions.py` | Real bank/broker detection tokens for all 7 parsers |
| `private/allowlist.local.txt` | Real names allowed through privacy filter |
| `private/wash_sale_adjustments.csv` | Wash-sale disallowance amounts (needed for W16) |
| `tests/fixtures/private/` | Real PDF fixtures for parser integration tests |

### Runtime health preflight (consultant/dev-agent workaround)

Before concluding parser or import logic is broken, run this preflight:

```bash
python -c "import importlib.util; print('pdfplumber_installed', importlib.util.find_spec('pdfplumber') is not None)"
python -c "from pathlib import Path; print('private_institutions_exists', Path('private/institutions.py').exists())"
FI_DB_PATH=/tmp/ledger_agent_preflight.db python -c "from pathlib import Path; import ledger_agent.core.api as api; r=api.import_statements(Path('/Users/vn53fda/Downloads/TinyProject/statements'), allow_partial=True); print('imported', r.imported, 'skipped', r.skipped, 'failed', r.failed)"
```

Classify status explicitly:
- `READY` — dependency and private config present; import runs.
- `BLOCKED-ENV` — runtime dependency missing.
- `BLOCKED-PRIVATE-CONFIG` — private config missing.
- `FAILED-PARSER` — environment/config healthy but parse still fails.

---

## 3. Architecture in One Page

```
statements/ (PDF inputs)
    └─ parsed by ledger_agent/core/parsers/<parser>.py
           │  ┌ BaseStatementParser
           │  └ ParserRegistry (auto-registered via @ParserRegistry.register)
           ↓
ledger_agent/core/database.py   ← SQLite, SCHEMA_VERSION=6, WAL mode
    EntityRepo / AccountRepo / TransactionRepo / SnapshotRepo / PositionRepo / COARepo
           ↓
ledger_agent/core/intelligence/classifier.py
    classify_batch()  →  TransactionRepo.update_coa_with_meta()
           ↓
ledger_agent/core/accounting/
    balance_sheet.py   ←  BalanceSheetBuilder → BalanceSheet (with coverage manifest)
    continuity.py      ←  check_period_continuity / list_discontinuities
    tax_estimator.py
           ↓
ledger_agent/core/reports/renderer.py
    export_balance_sheet_json()  writes *.json + *.coverage.json
           ↓
ledger_agent/core/api.py   ← 6 public functions consumed by all four forms
```

**Key invariants**:
- Every schema change bumps `SCHEMA_VERSION` and adds a migration block in `init_db()`.
- Every parser must implement `can_parse(text) -> bool` using only tokens from `private/institutions.py`.
- Every classified transaction gets `coa_code`, `classifier_version`, and `confidence` written back at classify time (not at report time).
- The redaction scanner (`scripts/check_doc_redaction.py --strict`) must exit 0 after every commit.

---

## 4. Current State (2026-05-25)

### ✅ Done and tested

| Feature | Files | Tests |
|---|---|---|
| `PositionType` enum + schema v4 migration | `models.py`, `database.py`, `broker_y_brokerage.py`, `broker_z.py` | `test_position_completeness.py` |
| Balance sheet coverage manifest (`consumed_snapshots` / `skipped_snapshots`) | `balance_sheet.py:138-173`, `renderer.py` | `test_aggregation_no_silent_drop.py` |
| Classification persistence (`classifier_version`, `confidence` columns; schema v5) | `classifier.py`, `database.py` | `test_classification_persisted.py` |
| Margin loan as 2010 liability (not contra-asset) | `balance_sheet.py:268-281` | `test_liability_recognition.py` |
| 2024 CPA parity — `test_total_income` PASS | `database.py:396` (dropped "deposit" from 4020 keywords) | `test_2024_cpa_parity.py` |
| Redaction scanner + pre-commit hook + CI gate | `scripts/check_doc_redaction.py`, `.pre-commit-config.yaml`, `.github/workflows/redaction-scan.yml` | — |
| Browser-popup runner | `scripts/run_with_browser.py` | — |

### ⚠️ Partially done / has known bugs

| Item | Status | Fix location |
|---|---|---|
| `bank_x4_checking.py` — "Customer Deposits" section | Jan + Feb 2026 ACH deposits silently missed | `docs/developer-tasks.md` TASK-2 |
| 3 CPA parity tests (`test_total_deductions`, `test_ordinary_business_income`, `test_p1_ordinary_income`) | `@xfail` — COGS structural fix needed | W15-COGS-LINE |
| `test_net_stcg` | `@xfail` — wash-sale disallowance not implemented | W16-WASH-SALE (after W15) |
| 3 `TestBalanceSheetParity` tests | `@skip` — missing 2024 `account_snapshots` rows in dev DB | W17-DATA-REFRESH |
| `private/institutions.py` | Not on disk → all 7 parsers return `can_parse=False` | Owner must create from template |

### ⛔ Blocked (spec required from owner)

| Ticket | Name | What is already on disk |
|---|---|---|
| ARCH-29 | Partner-withholding reclassification for pass-through entities | Nothing |
| ARCH-30 | Entity isolation in export artefacts | Nothing |
| ARCH-31 | Fiscal-year carry-forward audit | `accounting/continuity.py`, `tests/unit/test_continuity.py` — detection done; write-back not done |
| ARCH-32 | CPA-parity golden integration test | `tests/integration/test_2024_cpa_parity.py` (partial, 3 xfail) |

**The full spec for ARCH-29..32 was in `requirement-and-review-feedback.md §4` but that section is missing from the current document.** Owner must restore it or supply ticket bodies before these can be implemented.

---

## 5. Open Task List (prioritised)

### Wave 0 — Immediate (unblock 2026 statement parsing)

#### TASK-1 · Populate `private/institutions.py` · 5 min · P0
```python
# private/institutions.py
BANK_X4 = {"detect": ["U.S. BANK"]}   # works for both checking and credit card
```
All other tokens (`BANK_X`, `BANK_X2`, `BANK_X3`, `BROKER_Y`, `BROKER_Z`) must already be set for 2024–2025 statements to continue importing.  
**Acceptance**: `BankX4CheckingParser.can_parse(march_2026_text) == True`

#### TASK-2 · Fix "Customer Deposits" section in `bank_x4_checking.py` · ~1 hr · P1
Exact patch and regex documented in `docs/developer-tasks.md § TASK-2`.  
**Acceptance**: Jan 2026 parse returns ≥1 transaction; Feb 2026 returns $2,400 ACH + $600 Ext Tfr.

#### TASK-3 · Integration smoke test for all 8 USB 2026 PDFs · ~1 hr · P1
Full test scaffold in `docs/developer-tasks.md § TASK-3`.  
File: `tests/integration/parsers/test_bank_x4_2026.py`  
**Acceptance**: All 8 PDFs parse; auto-skipped in CI without `private/institutions.py`.

### Wave 1 — Open-source prep (parallel, all independent)

| Lane | Goal | Key files | Effort |
|---|---|---|---|
| **W15-COGS-LINE** | Fix `generate_form_1065` COGS — flip 3 xfail → PASS | `api.py:306-322`, `models.py`, new `test_form_1065_cogs.py` | M |
| **W17-DATA-REFRESH** | Rebuild 2024 `account_snapshots` — flip 3 skip → PASS | new `scripts/rebuild_snapshots.py`, fixture JSON | M |
| **W19-CONTRIBUTING-DOCS** | CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md, issue templates | 6 new files | S |
| **W20-MCP-DEMO** | End-to-end MCP server demo | `examples/mcp/`, `scripts/mcp_smoke.py` | S |
| **W22-CI-RELEASE-DRY-RUN** | Trigger dry-run; audit lockfile for Walmart `--index-url` (**hard blocker for public CI**) | `docs/ci-dry-run-evidence.md` | S |
| **W23-SECURITY-WORKFLOWS** | CodeQL + Dependabot + pip-audit | 3 new workflow files | S |
| **W24-ENV-EXAMPLE-AUDIT** | Verify `.env.example` covers every `os.environ` read | `.env.example`, `docs/env-vars.md`, new arch test | S |
| **W25-LICENSE-NOTICE** | Audit third-party licences — block if any GPL transitive dep | `NOTICE`, `THIRD_PARTY_LICENSES.md`, `scripts/check_licenses.py` | S |

### Wave 2 — After Wave 1

| Lane | Goal | Depends on |
|---|---|---|
| **W16-WASH-SALE** | Implement wash-sale disallowance | W15 (`api.py` edit) |
| **W21-WEBAPP-DEMO** | Spring Boot demo + docker-compose | W20 pattern |

### Wave 3 — Release gate (owner-driven, destructive)

| Lane | Goal |
|---|---|
| **W5-SQUASH** | Pre-publish history rewrite in a fresh `--mirror` clone (see `docs/history-audit.md`) |

### Final wave — Ship

| Lane | Goal | Gates |
|---|---|---|
| **W18-FIRST-PUBLIC-RELEASE** | Tag `v0.1.0`, cut 6 artifacts | W5 + W15 + W17 + W22 + W19 all DONE; scanner 0 hits; suite green |

---

## 6. Rules Every Contributor Must Follow

1. **Redaction scanner must stay at 0 hits** after every commit.  
   `python scripts/check_doc_redaction.py --strict --all-tracked`  
   Three-tier suppression when it fires: pseudonymise → `# redaction: allow` → `exempt_files`.

2. **Schema changes**: bump `SCHEMA_VERSION` in `database.py` and add a migration block in `init_db()`. Never rename columns — add new ones.

3. **Parser detection tokens** live only in gitignored `private/institutions.py`. Committed code falls back to `{"detect": []}` if absent.

4. **Classification must be persisted at classify time** — use `TransactionRepo.update_coa_with_meta()`, never classify at report time.

5. **Balance sheet coverage** — every account must either appear in `consumed_snapshots` or `skipped_snapshots`. Silent drops are forbidden.

6. **Four-form parity** — any change to the core engine must not break `pytest tests/integration/test_2024_cpa_parity.py` (beyond existing xfail annotations).

7. **Pseudonym corpus** (`config/redaction_corpus.yaml`) is the canonical source of truth. Use only:  
   `ENTITY_A`, `ENTITY_B`, `PARTNER_1`, `PARTNER_2`, `BANK_X`, `BANK_X2`, `BANK_X3`, `BANK_X4`, `BROKER_Y`, `BROKER_Z` in committed code and docs.

---

## 7. Key File Map

| Need | File |
|---|---|
| Project architecture contract | `AGENTS.md` |
| Full work history and W-lane record | `requirement-and-review-feedback.md` |
| Developer task cards (TASK-1..4) | `docs/developer-tasks.md` |
| Pseudonym corpus | `config/redaction_corpus.yaml` |
| Redaction scanner | `scripts/check_doc_redaction.py` |
| All parsers | `ledger_agent/core/parsers/` |
| Parser detection tokens (gitignored) | `private/institutions.py` (template: `private/institutions.example.py`) |
| Core API (6 public functions) | `ledger_agent/core/api.py` |
| Database + schema migrations | `ledger_agent/core/database.py` |
| Balance sheet builder | `ledger_agent/core/accounting/balance_sheet.py` |
| Transaction classifier | `ledger_agent/core/intelligence/classifier.py` |
| Report renderer | `ledger_agent/core/reports/renderer.py` |
| Carry-forward checker | `ledger_agent/core/accounting/continuity.py` |
| MCP tool schemas | `ledger_agent/mcp/tools.py` |
| CPA parity fixture | `tests/integration/fixtures/2024_cpa_expected.json` |
| Parity divergence diagnostic | `docs/parity-divergence.md` |
| W9 deductions diagnostic | `docs/w9-deductions-diagnostic.md` |
| Release pipeline | `docs/release.md` |
| Hashed lockfile | `requirements.lock` |
| Browser runner | `scripts/run_with_browser.py` |
| Customer-readable summary contract | `docs/customer-summary-contract.md` |

---

## 8. Questions for the Owner Before Starting ARCH-29..32

The full ticket bodies for these four items were lost when `requirement-and-review-feedback.md §4` was truncated. Before any work begins, the owner must answer one of the following for each ticket:

| Ticket | Minimum info needed |
|---|---|
| **ARCH-29** | What is the reclassification rule? Which COA code(s)? Which transaction patterns trigger it? What is the audit event name? |
| **ARCH-30** | What does "entity isolation" mean concretely? Separate export folders per `entity_id`? WHERE-filtered queries? Both? |
| **ARCH-31** | Does the ticket require materialising `PRIOR_PERIOD_ADJUSTMENT` transactions, or just detecting and reporting the delta? |
| **ARCH-32** | Which periods form the "golden" fixture (2024-12 → 2025-01 → 2025-12)? What Schedule L lines must match? What is the `$1` tolerance? |

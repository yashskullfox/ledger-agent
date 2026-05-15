# ledger-agent — Architecture & Structure

> Field guide for contributors, AI agents, and developers extending the system.
> Covers the four-form architecture (R-50), release pipeline (R-51), and all
> twelve ARCH tickets landed in v2.1.0.

---

## Philosophy

**One engine, four surfaces.  Local-first.  Privacy by default.**

1. All parsing, classification, and accounting runs entirely on the local machine — zero required cloud dependency.
2. `ledger_agent.core.api` is the **single source of truth** for all six operations. Forms B, C, and D are thin wrappers — they never reimplement logic.
3. The MCP server and Java bridge reuse the same transport framing (newline-delimited JSON-RPC 2.0) so transport tests apply to both.
4. Remote AI (OpenAI, Gemini) is an optional second-pass escalation for low-confidence classifications only.
5. Every institution is a self-contained plugin. Adding a new bank requires exactly one file.

---

## Repository Layout

```
ledger-agent/
│
├── run.sh                    Bootstrap launcher (Unix) — creates .venv, installs, dispatches
├── run.bat                   Bootstrap launcher (Windows) — same behaviour
├── main.py                   Legacy interactive menu (pass-through for mcp/context/classify/…)
├── config.py                 All configuration via environment variables
├── pyproject.toml            Root package metadata (ledger-agent 2.1.0)
├── requirements.txt          Loose runtime deps
├── requirements.lock         Pinned deps for reproducible builds (ARCH-11)
├── requirements-dev.txt      pytest, ruff, mypy
│
├── ledger_agent/             ── Python package root ──────────────────────────────────
│   ├── __init__.py           __version__ = "2.1.0"
│   │
│   ├── core/                 Form A — Pure core library (ARCH-01/02/03)
│   │   ├── __init__.py
│   │   └── api.py            Six public functions + return-type dataclasses
│   │                         import_statements · generate_balance_sheet · generate_form_1065
│   │                         generate_k1 · pte_estimate · reconcile_year
│   │
│   ├── cli/                  Form B — Thin CLI layer (ARCH-04)
│   │   ├── __init__.py
│   │   └── main.py           app() entrypoint; delegates 100% to core.api
│   │
│   ├── mcp/                  Form C — Spec-compliant MCP server (ARCH-06/07)
│   │   ├── __init__.py
│   │   ├── __main__.py       python -m ledger_agent.mcp
│   │   ├── server.py         _dispatch(), serve_stdio(), serve_http(), main()
│   │   ├── tools.py          TOOL_SCHEMAS + call_tool() → core.api
│   │   ├── transport_stdio.py Newline-delimited JSON-RPC I/O helpers
│   │   ├── transport_http.py  ASGI app for streamable-HTTP transport
│   │   └── manifest.json     MCP registry manifest (R-50 Form C)
│   │
│   └── bridge/               Java↔Python JSON-RPC bridge (ARCH-08)
│       ├── __init__.py
│       └── jsonrpc_stdio.py  Stdio JSON-RPC server; reuses mcp.tools.call_tool
│
├── core/                     SQLite repos, models, privacy, logging (shared by all forms)
│   ├── models.py             Entity, Account, Transaction, Position, AccountSnapshot
│   ├── database.py           EntityRepo, TransactionRepo, AccountRepo, SnapshotRepo, …
│   ├── privacy.py            R-46 PII firewall — redact / unredact / audit_egress
│   ├── exceptions.py         Custom exception hierarchy
│   └── logging_setup.py      Structured logging: rich | json | plain
│
├── parsers/                  Statement PDF parser plugins (auto-discovered)
│   ├── __init__.py           pkgutil.iter_modules auto-discovery
│   ├── base.py               BaseStatementParser ABC
│   ├── registry.py           @ParserRegistry.register + auto-detect
│   ├── bank_x_checking.py
│   ├── broker_y_brokerage.py
│   ├── bank_x3_checking.py
│   ├── bank_x2_checking.py
│   ├── bank_x4_checking.py
│   ├── bank_x4_creditcard.py
│   └── broker_z.py
│
├── intelligence/             Classification and learning layer
│   ├── classifier.py         5-step pipeline: memory → local → AI → keywords → prompt
│   ├── memory.py             JSON-backed persistent classification rules
│   ├── reconciler.py         Inter-account transfer matching
│   └── ai_backend/
│       ├── __init__.py       Factory: local | openai | gemini → ChainedBackend
│       ├── base.py           AIBackend abstract interface
│       ├── chained_backend.py Local-first wrapper; escalates to remote on low confidence
│       ├── local_backend.py  Rule regex + rapidfuzz fuzzy match (zero API cost)
│       ├── openai_backend.py GPT-4o-mini via OpenAI Chat Completions
│       └── gemini_backend.py Google Gemini 1.5 Flash
│
├── accounting/               Financial statement builders
│   ├── balance_sheet.py      BalanceSheetBuilder → GAAP-style balance sheet
│   └── tax_estimator.py      Quarterly 1040-ES estimator (SE + federal + state + QBI)
│
├── reports/
│   └── renderer.py           Rich console output + CSV / Excel / JSON export
│
├── adapters/
│   └── context_builder.py    Serialises financial data for Claude / GPT / Perplexity
│
├── mcp_server/               Legacy MCP server (kept for backward compat; superseded by ledger_agent/mcp/)
│   └── server.py             Old LSP-framed server — do not use for new integrations
│
├── cli/                      Legacy CLI helpers (used by main.py pass-through)
│   ├── commands.py
│   ├── onboarding.py         R-45 Coverage Wizard (12-month gap analysis)
│   ├── quick_scan.py
│   └── prompts.py
│
├── packaging/                Per-form pyproject.toml manifests (ARCH-03)
│   ├── core/
│   │   ├── pyproject.toml    ledger-agent-core wheel
│   │   └── README.md
│   ├── cli/
│   │   └── pyproject.toml    ledger-agent-cli wheel
│   └── mcp/
│       └── pyproject.toml    ledger-agent-mcp wheel
│
├── webapp/                   Form D — Spring Boot 3 mini-webapp (ARCH-08/09/10)
│   ├── pom.xml               Maven 3.9.8, Spring Boot 3.3, JDK 21
│   ├── .mvn/wrapper/         Pinned maven-wrapper.properties
│   └── src/main/
│       ├── java/com/ledgeragent/
│       │   ├── LedgerAgentApplication.java   Spring Boot entry point
│       │   ├── bridge/
│       │   │   ├── PythonBridge.java          Subprocess lifecycle + typed API
│       │   │   ├── JsonRpcClient.java         JSON-RPC 2.0 stdio client
│       │   │   └── BridgeException.java
│       │   ├── web/
│       │   │   └── RunController.java         GET / POST /run GET /healthz
│       │   └── runtime/
│       │       └── PythonRuntimeExtractor.java  Unpack CPython from jar on first run
│       └── resources/
│           ├── application.yml
│           └── templates/
│               ├── index.html     Folder picker + fiscal year + report selector
│               └── results.html   JSON results + inline error + next-step panel
│
├── tests/
│   ├── conftest.py           Shared fixtures; pytest_configure sets FI_DB_PATH before collection
│   ├── architecture/
│   │   └── test_core_purity.py  ARCH-02: zero CLI/UI imports in core (32 tests)
│   ├── integration/
│   │   ├── test_mcp_privacy.py  ARCH-07: privacy firewall (13 tests)
│   │   └── test_2024_cpa_parity.py  ARCH-12: CPA parity gate (15 tests; skip w/o corpus)
│   ├── test_balance_sheet.py
│   ├── test_classifier.py
│   ├── test_models.py
│   ├── test_onboarding.py
│   ├── test_parsers.py
│   ├── test_privacy.py
│   └── test_tax_estimator.py
│
├── .github/
│   ├── workflows/
│   │   └── release.yml       ARCH-11/12: four-artifact release pipeline
│   └── scripts/
│       ├── compute_semver.py  Conventional-commit semver calculator
│       └── sha256sums.sh      SHA256SUMS generator for release artifacts
│
└── data/                     ← NOT committed (gitignored)
    ├── statements/            PDF statements
    ├── db/
    │   ├── financials.db      SQLite database
    │   └── classification_memory.json
    └── exports/               CSV / Excel / JSON exports
```

---

## Dependency Graph (ARCH tickets)

```
ARCH-01 ── ARCH-02 ── ARCH-03 ─┬─ ARCH-04 ── ARCH-05 ────────────────────────┐
                                │                                               │
                                └─ ARCH-06 ─┬─ ARCH-07 ──────────────────────┐│
                                            │                                 ││
                                            └─ ARCH-08 ── ARCH-09 ── ARCH-10 ┘│
                                                                               │
                                                              ARCH-11 ◄────────┘
                                                                 │
                                                              ARCH-12
```

---

## Data Flow

```
Coverage Discovery (R-45: ./run.sh scan)
  │  Resolve folder → discover PDFs → probe each (parser + period + account)
  │  Build 12-month coverage matrix → render ✅/⚠/❌ table
  │  Gap-fill interactive loop
  ▼
PDF file  →  parsers/registry.py (auto-detect)  →  BaseStatementParser.parse()
                                                         │
                                              ParsedStatement
                                              ├── transactions: List[Transaction]
                                              ├── positions:    List[Position]
                                              └── snapshot:     AccountSnapshot
                                                         │
                                              core/database.py (persist to SQLite)
                                                         │
                                              intelligence/classifier.py (5-step)
                                              1. Parser pre-classification
                                              2. memory.lookup() (rapidfuzz WRatio ≥ 85)
                                              3. AI backend (local → remote on low conf.)
                                              4. COA keyword scan
                                              5. Interactive prompt → saved to memory
                                                         │
                              ┌────────────────────────────────────────────────┐
                              │         ledger_agent.core.api                  │
                              │  ┌──────────┐  ┌──────────┐  ┌─────────────┐  │
                              │  │ Form B   │  │ Form C   │  │  Form D     │  │
                              │  │ (CLI)    │  │ (MCP)    │  │  (Spring)   │  │
                              │  └──────────┘  └──────────┘  └─────────────┘  │
                              └────────────────────────────────────────────────┘
                                         │
                              accounting/balance_sheet.py  → BalanceSheet
                              accounting/tax_estimator.py  → TaxEstimate
                              reports/renderer.py          → console / CSV / JSON
```

---

## Core API (Form A) — `ledger_agent.core.api`

The **only** stable public surface. All other forms call these six functions:

```python
import ledger_agent.core.api as api
from pathlib import Path

# Import PDFs (idempotent)
report = api.import_statements(Path("~/statements"), allow_partial=False)

# Year-end reporting
bs   = api.generate_balance_sheet(2024)   # → BalanceSheet
f    = api.generate_form_1065(2024)       # → Form1065
k1y  = api.generate_k1(2024, "yash")     # → ScheduleK1
k1p  = api.generate_k1(2024, "parin")    # → ScheduleK1
est  = api.pte_estimate(2024)             # → PTEEstimate
rec  = api.reconcile_year(2024)          # → ReconcileReport
```

**Accept test (ARCH-01):**
```bash
python -c "
import ledger_agent.core.api as a
[getattr(a,f) for f in ['import_statements','generate_balance_sheet',
 'generate_form_1065','generate_k1','pte_estimate','reconcile_year']]
"
```

---

## Core Purity Rule (ARCH-02)

Directories `core/`, `accounting/`, `intelligence/`, `parsers/`, `reports/`, `ledger_agent/core/`
must **never** import:

```
cli  rich  click  typer  requests  httpx  fastapi  flask  questionary  colorama
```

Verified by:
```bash
pytest tests/architecture/test_core_purity.py -q   # 32 tests
```

Exceptions: `reports/renderer.py` (legitimately uses `rich`) and `logging_setup.py`.

---

## MCP Server (Form C) — `ledger_agent.mcp`

**Protocol:** MCP 2024-11-05 (JSON-RPC 2.0 over stdio or streamable-HTTP).

**Tools:** Six tools mapping 1:1 to `core.api`. Schemas in `ledger_agent/mcp/tools.py`.

**Privacy:** Every tool response passes through `_redact_response()` in `server.py`.
The caller can opt out with `_meta: { allow_pii: true }` in the tool call.

**Accept test (ARCH-06):**
```bash
python -c "
from ledger_agent.mcp.tools import TOOL_SCHEMAS
assert len(TOOL_SCHEMAS) == 6
"
```

**Privacy test (ARCH-07):**
```bash
pytest tests/integration/test_mcp_privacy.py -q   # 13 tests
```

---

## Java Webapp (Form D) — `webapp/`

Architecture: Spring Boot 3.3 → `PythonBridge` bean → subprocess → `ledger_agent.bridge.jsonrpc_stdio`.

**Startup sequence:**
1. `PythonRuntimeExtractor.extractToTempDir()` unpacks embedded CPython (if running from fat jar).
2. `PythonBridge.afterPropertiesSet()` starts `python -m ledger_agent.bridge.jsonrpc_stdio`.
3. `PythonBridge.ping()` verifies the subprocess is responsive.
4. Spring Boot serves `http://localhost:8080`.

**Build:**
```bash
cd webapp && ./mvnw package -DskipITs
java -jar target/ledger-agent-webapp-*.jar
```

**Accept test (ARCH-09):**
```bash
cd webapp && ./mvnw spring-boot:run & sleep 6 && \
  curl -fsS http://127.0.0.1:8080/ | grep -q "fiscal year"; kill %1
```

---

## Release Pipeline (ARCH-11/12)

File: `.github/workflows/release.yml`

**Jobs in dependency order:**
```
compute-version
  ├── build-core           (Form A zip)
  ├── build-cli            (Form B tarball + run.sh + run.bat)
  ├── build-mcp            (Form C zip + manifest.json)
  └── build-webapp-{linux,macos,windows}  (Form D fat jars)
        │
     smoke (architecture + MCP privacy tests)
     parity-gate (CPA 2024 parity — blocks release if numbers diverge > $1)
        │
     release (GitHub Release + SHA256SUMS)
```

**Semver:** computed from conventional commits by `.github/scripts/compute_semver.py`.
`feat:` → minor bump, `fix:` → patch, `feat!:` / `BREAKING CHANGE:` → major.

**Reproducible builds:** `SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)`, Python 3.11.9,
JDK 21 temurin, `pip install --require-hashes -r requirements.lock`.

---

## Adding a New Institution Parser

Create `parsers/wells_fargo.py`:

```python
from parsers.base import BaseStatementParser
from parsers.registry import ParserRegistry
from core.models import ParsedStatement, StatementType
from pathlib import Path

@ParserRegistry.register
class WellsFargoParser(BaseStatementParser):
    PARSER_ID   = "wells_fargo"
    INSTITUTION = "Wells Fargo"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        return "WELLS FARGO" in text.upper() and "BUSINESS" in text.upper()

    def parse(self, pdf_path: Path) -> ParsedStatement:
        raw_text = self.extract_text(pdf_path)
        # ... extract period, account number, transactions ...
        return ParsedStatement(parser_id=self.PARSER_ID, ...)
```

**That is all.** `parsers/__init__.py` uses `pkgutil.iter_modules` to auto-discover the file.
No edits to any other file are needed.

---

## Configuration Reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `FI_AI_BACKEND` | `local` | `local` / `openai` / `gemini` |
| `FI_DB_PATH` | `data/db/financials.db` | SQLite database path |
| `FI_STATEMENTS_DIR` | `data/statements/` | Default statements folder |
| `FI_AI_EGRESS_MODE` | `redact` | `redact` / `strict` / `mock` / `passthrough` |
| `FI_OPENAI_API_KEY` | — | Required if backend=openai |
| `FI_GEMINI_API_KEY` | — | Required if backend=gemini |
| `FI_AUTO_CLASSIFY_THRESHOLD` | `85` | Memory fuzzy-match threshold |
| `FI_LOCAL_CONFIDENCE_THRESHOLD` | `0.65` | Escalation threshold to remote AI |
| `FI_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `FI_SE_TAX_RATE` | `0.153` | Self-employment tax rate |
| `FI_FED_INCOME_RATE` | `0.22` | Federal income tax rate |
| `FI_STATE_TAX_RATE` | `0.05` | Missouri PTE rate |
| `FI_QBI_DEDUCTION` | `0.20` | QBI deduction |
| `LEDGER_PYTHON_HOME` | — | Extracted Python home for Form D fat jar |

---

## Running Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt

pytest                                                          # full suite (207 tests)
pytest tests/architecture/test_core_purity.py -q               # ARCH-02 (32)
pytest tests/integration/test_mcp_privacy.py -q                # ARCH-07 (13)
pytest -m parity tests/integration/test_2024_cpa_parity.py -q  # ARCH-12 (skip w/o corpus)
pytest --cov=. --cov-report=term-missing                        # coverage
```

---

## Security Model

- `data/` is gitignored — statements, database, and exports never commit.
- `.env` is gitignored — API keys never committed.
- `config.py` scans itself at import for accidentally committed keys.
- Account numbers stored as last-4 masked strings only.
- **R-46 PII firewall** — `core/privacy.py` tokenises PII before any remote call; fully active on MCP egress.
- **R-45 completeness gate** — year-end outputs block when a month × institution is missing.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `core.api` as sole public surface | Prevents Forms B/C/D from diverging; single parity test covers all |
| Subprocess JSON-RPC for Java bridge | Avoids Py4J gateway sockets and GraalVM CPython-extension incompatibility |
| Three classifier fat jars (linux/darwin/windows) | OS-specific CPython distros inflate jar; boot-time detection picks correct one |
| `requirements.lock` with `--require-hashes` | Reproducible CI builds; byte-identical artifacts across runs |
| Conventional commits for semver | Human-readable, no manual tag management; `feat!:` auto-bumps major |
| Privacy firewall default-on | MCP clients may tunnel to cloud models; safer to redact by default |

---

*Last updated: May 2026 — v2.1.0 (ARCH-01 through ARCH-12 complete)*

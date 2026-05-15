# AGENTS.md — ledger-agent

> **Privacy notice.** Pseudonyms used throughout: `ENTITY_A` (the partnership
> under test), `PARTNER_1` / `PARTNER_2`, `BANK_X` (and variants `BANK_X2`,
> `BANK_X3`, `BANK_X4`), `BROKER_Y` / `BROKER_Z`, `TICKER_SEC1` / `TICKER_SEC2`,
> `acct_****1234`. Real values live only in `data/` and `statements/` (both
> gitignored) and in `private/pseudonym-map.local.md` (gitignored, may not yet
> exist on a fresh clone). Do **not** paste real names, account numbers, or
> dollar figures into this file.
>
> *Last reviewed 2026-05-15. This file is the agent-facing entry contract.
> It supersedes nothing. Read `requirement-and-review-feedback.md` for the
> canonical ticket status board.*

---

## 1. Read me first (if you are an agent)

This is the operating contract for any AI coding agent — Claude Code, Cursor,
Aider, Codex CLI, Wibey, others — that opens this repository. Before you
touch a file, internalise three rules:

1. **Never claim ✅ on a row, ticket, or acceptance criterion without
   on-disk evidence.** Verify with `ls`, `grep`, or a passing test. The
   2026-05-14 hardening pass enumerated ~19 rows in
   `requirement-and-review-feedback.md` that had been marked ✅ without the
   code being on disk; every one had to be re-opened. Repeating that pattern
   is the worst failure mode in this codebase.
2. **The privacy contract (R-73 / R-74) overrides everything else.** No real
   entity, partner, bank, broker, ticker, account number, or cent-precision
   figure may land in any tracked artefact. Use the pseudonym corpus in
   `config/redaction_corpus.yaml`. The real-name map lives in
   `private/pseudonym-map.local.md` (gitignored — and on a fresh clone, the
   file may not yet exist).
3. **The PII firewall fails closed.** No raw PII may leave the host without
   explicit `allow_pii=True` opt-in. Egressing PII accidentally — to a model
   provider, to an audit log, to stdout, to disk outside `data/` — is a P0
   incident.

Where to look first:

- `requirement-and-review-feedback.md` — canonical status board. **Always**
  consult §2 (status board) before claiming a ticket is done; §3.1 is the
  open-bug inventory.
- `STRUCTURE.md` — architecture and module layout. Note that its Repository
  Layout still references several top-level directories (`core/`,
  `accounting/`, `intelligence/`, `parsers/`, `reports/`) that no longer
  exist as top-level — they are under `ledger_agent/core/` and
  `ledger_agent/`. The doc is in the process of being reconciled.
- `README.md` — user-facing entry points and CLI reference.
- `docs/redaction-policy.md` — the privacy/redaction policy and allowlist
  syntax.
- `config/redaction_corpus.yaml` — authoritative pseudonym corpus.

---

## 2. Project at a glance

`ledger-agent` is a local-first partnership-accounting engine for a US LLC
filing **Form 1065** (`ENTITY_A`, two partners `PARTNER_1` and `PARTNER_2`).
It reads bank/brokerage PDF statements, classifies transactions, and produces
balance sheets, income statements, Schedule K-1s, Form 1065 line items, and
quarterly PTE tax estimates.

It ships as **four forms** built from one core library (R-50):

| Form | What | Entry point |
|---|---|---|
| A — Core library | Pure-Python engine | `import ledger_agent.core.api` |
| B — CLI | Interactive terminal runner | `ledger` · `./run.sh` |
| C — MCP server | Spec-compliant MCP (stdio + streamable-HTTP) | `ledger-agent-mcp` |
| D — Spring Boot fat jar | Self-contained webapp (no Python on host) | `java -jar ledger-agent-webapp-*.jar` |

**All four forms must produce identical numbers for the parity fixture.**
Divergence > $1 is P0 (R-51 / ARCH-32).

---

## 3. Install / build / test / run

Verified against `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`,
`run.sh`, `.github/workflows/release.yml` as of 2026-05-15.

### 3.1 Python environment

- Python `>=3.10` (see `pyproject.toml` `[project] requires-python`).
- CI runs against **3.10 / 3.11 / 3.12**; release builds pin to **3.11.9**
  (see `.github/workflows/release.yml`).
- Install runtime + dev deps:

  ```bash
  pip install -r requirements.txt -r requirements-dev.txt
  ```

- Reproducible install (CI):

  ```bash
  pip install --require-hashes -r requirements.lock
  ```

### 3.2 Running the CLI (Form B)

```bash
./run.sh scan ~/Downloads/statements/      # coverage wizard + import
./run.sh balance 2024                      # year-end balance sheet
./run.sh form1065 2024                     # Form 1065 summary
./run.sh k1 2024 --partner partner_1       # Schedule K-1
./run.sh tax 2024                          # quarterly PTE estimate
./run.sh reconcile 2024                    # inter-account reconciliation
```

Console scripts registered by `pyproject.toml`:

- `fi` → `main:main` (legacy menu)
- `ledger` → `ledger_agent.cli.main:app`
- `ledger-agent-mcp` → `ledger_agent.mcp.server:main`

### 3.3 Running the MCP server (Form C)

```bash
ledger-agent-mcp                          # stdio (default)
ledger-agent-mcp --transport http         # streamable-HTTP on :7337
python -m ledger_agent.mcp                # equivalent
```

Pass `_meta: { allow_pii: true }` in a tool call to bypass the PII redaction
firewall. This is logged via `audit("mcp.redaction.skipped", ...)`.

### 3.4 Building the Spring Boot webapp (Form D)

```bash
cd webapp && ./mvnw package -DskipITs
java -jar target/ledger-agent-webapp-*.jar
```

JDK 21 (temurin) per `webapp/pom.xml` and `release.yml`.

### 3.5 Tests

```bash
pytest                                                          # full suite
pytest tests/architecture/test_core_purity.py -q                # ARCH-02
pytest tests/integration/test_mcp_privacy.py -q                 # ARCH-07
pytest -m parity tests/integration/test_2024_cpa_parity.py -q   # parity (skipped without corpus)
```

Test markers (declared in `pyproject.toml`):

- `parity` — CPA parity tests; require `FI_CPA_CORPUS_PATH` env var or the
  default `statements/2024.txt` (gitignored). Skip gracefully when absent.
- `integration` — integration tests; require a populated DB.

The parity tolerance is `Decimal("1.00")` — any divergence above $1 fails.

### 3.6 Linters and type-checks

- `ruff` (config in `pyproject.toml [tool.ruff]`): line-length 100, target
  `py310`, selected rule sets `E F W I N UP`, ignores `E501`.
- `mypy` (config in `[tool.mypy]`): `python_version = "3.10"`,
  `ignore_missing_imports = true`, `warn_return_any = true`.

There is **no `Makefile` on disk** as of 2026-05-15. The documented
`make check-docs` and `make install-hooks` targets referenced in
`docs/redaction-policy.md` and in `requirement-and-review-feedback.md`
acceptance criteria are **aspirational** — they are part of the ARCH-43 /
ARCH-35 doc-privacy track and have not landed. Run the scanner directly:

```bash
python scripts/check_doc_redaction.py --all-tracked
python scripts/check_doc_redaction.py --staged
python scripts/check_doc_redaction.py --paths README.md STRUCTURE.md
```

---

## 4. Repository structure (what actually lives where)

The canonical narrative is in `STRUCTURE.md`; the snapshot below is the
**verified on-disk view** as of 2026-05-15. Where it disagrees with
`STRUCTURE.md`, the on-disk view wins.

```
ledger-agent/
├── ledger_agent/                  Python package root (__version__ = "2.1.0")
│   ├── core/                      Form A — core engine + privacy + audit + cleanup
│   │   ├── api.py                 The six public functions (single source of truth)
│   │   ├── accounting/            balance_sheet.py · continuity.py · tax_estimator.py
│   │   ├── parsers/               base.py · registry.py · {bank_x,bank_x2,bank_x3,
│   │   │                          bank_x4}_checking.py · bank_x4_creditcard.py ·
│   │   │                          broker_y_brokerage.py · broker_z.py
│   │   ├── intelligence/          classifier + ai_backend/
│   │   ├── reports/               renderer.py (rich/CSV/Excel/JSON)
│   │   ├── privacy.py             R-46 PII firewall; detectors + redact()
│   │   ├── audit.py               Append-only JSONL audit log (0o600)
│   │   ├── cleanup.py             run_cycle() + boot_cleanup() + scratch sweep
│   │   ├── database.py            SQLite repos; SCHEMA_VERSION = 5
│   │   ├── models.py              Entity / Account / Transaction / Position / Snapshot
│   │   └── privacy_allowlist.txt  In-code redaction allowlist
│   ├── cli/main.py                Form B entry (app())
│   ├── mcp/                       Form C — server.py · tools.py · transports · manifest.json
│   ├── bridge/jsonrpc_stdio.py    Java↔Python JSON-RPC bridge
│   └── data/audit/                Audit log destination (runtime)
│
├── adapters/context_builder.py    AI-context serialiser
├── cli/                           Legacy CLI helpers (used by main.py pass-through)
├── mcp_server/                    Legacy MCP server — do NOT use for new integrations
├── webapp/                        Form D Spring Boot 3 (pom.xml, src/, target/)
├── packaging/{core,cli,mcp}/      Per-form pyproject.toml manifests (R-50)
│
├── tests/
│   ├── conftest.py                Sets FI_DB_PATH before collection
│   ├── architecture/              test_core_purity.py (ARCH-02)
│   ├── integration/
│   │   ├── test_2024_cpa_parity.py        (ARCH-12 / ARCH-32)
│   │   ├── test_mcp_privacy.py            (ARCH-07)
│   │   ├── test_aggregation_no_silent_drop.py (ARCH-26)
│   │   ├── test_classification_persisted.py   (ARCH-27)
│   │   ├── fixtures/parsers/
│   │   └── fixtures/brokerage/<inst>/<period>/ (PDFs gitignored)
│   ├── unit/                      (NOTE: orphan __pycache__/ — no source files yet;
│   │                               test_k1_allocation.py claimed but absent — ARCH-19)
│   └── test_{balance_sheet,classifier,models,onboarding,parsers,privacy,tax_estimator}.py
│
├── scripts/
│   ├── check_doc_redaction.py     Doc-redaction scanner (ARCH-34)
│   └── regen_parity_corpus.py     CPA corpus regenerator (ARCH-21)
│
├── config/redaction_corpus.yaml   ARCH-33 pseudonym corpus
├── docs/redaction-policy.md       ARCH-33 policy doc
├── private/                       Gitignored. May only contain example.py on fresh clone.
├── statements/                    Gitignored. Real CPA corpus.
├── data/                          Gitignored. Statements, DB, exports, raw_cache.
│
├── .github/
│   ├── workflows/release.yml       Four-artefact release pipeline (ARCH-11)
│   ├── workflows/repro-check.yml   Byte-identical-build verifier
│   └── scripts/compute_semver.py   Conventional-commit semver calculator
│
├── pyproject.toml                 ledger-agent 2.1.0
├── requirements.txt / requirements.lock / requirements-dev.txt
├── run.sh / run.bat               Bootstrap launchers
├── main.py / config.py            Legacy menu + env-var config
├── README.md / STRUCTURE.md / DISCOVER.md
└── requirement-and-review-feedback.md   Canonical ticket status
```

**Things `STRUCTURE.md` references that are not actually on disk:**

- A top-level `core/` directory — gone; everything is under
  `ledger_agent/core/`.
- A top-level `parsers/`, `accounting/`, `intelligence/`, `reports/` — same;
  all moved under `ledger_agent/core/` via ARCH-20.
- `ledger_agent/migrations/` — does not exist. Migrations are inline in
  `ledger_agent/core/database.py` and version-gated by `SCHEMA_VERSION`.

If you are about to edit something based on `STRUCTURE.md`, run `ls` on the
path first. The on-disk truth wins.

---

## 5. The privacy contract (R-73 / R-74)

This is the single hardest rule in the repo.

### 5.1 Pseudonym corpus

Canonical list lives in `config/redaction_corpus.yaml`. The standing
pseudonyms used in tracked docs and tests are:

- Entity: `ENTITY_A`
- Partners: `PARTNER_1`, `PARTNER_2`
- Checking banks: `BANK_X`, `BANK_X2`, `BANK_X3`, `BANK_X4`
- Brokerages: `BROKER_Y`, `BROKER_Z`
- Tickers: `TICKER_SEC1`, `TICKER_SEC2`
- Account numbers: `acct_****` + last-4 (e.g. `acct_****1234`)
- Cent-precision figures within 5 tokens of a financial noun:
  `~$X,XXX` (no cents)
- Ownership percentages in prose: `<P1_pct>` / `<P2_pct>`

### 5.2 The local real-name map

Real-identifier mappings live exclusively in
`private/pseudonym-map.local.md`. This path is gitignored (see
`.gitignore` rule `private/`). **On a fresh clone, this file may not yet
exist** — currently only `private/institutions.example.py` is present. If
you need the real map, ask the human operator; do not regenerate or guess
it. Agent code MUST NOT read this file unless explicitly authorised in the
current task.

### 5.3 The scanner

`scripts/check_doc_redaction.py` reads the corpus and scans tracked files.
Run it before every commit that touches docs, tests, fixtures, or CI:

```bash
python scripts/check_doc_redaction.py --staged
python scripts/check_doc_redaction.py --all-tracked
python scripts/check_doc_redaction.py --paths <files...>
```

Exit code 0 = clean, 1 = at least one hit. Output names
`path:line:col: hit <category>` — it never echoes the matched value.

Allowlist syntax:

- Inline: append `# redaction: allow` to a line to suppress one hit.
- File-level: add a path pattern to `redaction.allowlist` (file not yet
  present on disk; create it only when needed).

### 5.4 Pre-commit hook

`.pre-commit-config.yaml` and a fallback `hooks/pre-commit` shell are
**aspirational** (ARCH-35, currently re-opened — see
`requirement-and-review-feedback.md` §2.3). Until they land, you are the
hook: run the scanner manually before staging.

### 5.5 Currently known doc-privacy leaks

These are tracked in `requirement-and-review-feedback.md` §4.4 ARCH-43 and
§3.3 DOC-3. Do not be surprised when the scanner flags them; do not
"helpfully" fix them as a side-quest — they need a coordinated rename pass:

- `README.md` and `STRUCTURE.md` reference real bank/broker brand strings
  and partner first names.
- `pyproject.toml`, `ledger_agent/__init__.py`, `ledger_agent/core/api.py`,
  `ledger_agent/core/database.py`,
  `ledger_agent/core/intelligence/ai_backend/local_backend.py`, and
  `ledger_agent/core/accounting/balance_sheet.py` all carry the real LLC
  name in comments/docstrings.
- `tests/integration/fixtures/2024_cpa_expected.json` contains real partner
  first names in category descriptions (ARCH-43 §NEW-1).
- `tests/integration/fixtures/brokerage/<brand>/...` subdirectories are
  named after real brokerage brands (ARCH-43 scope).

If your work touches any of those files, escalate via ARCH-43 rather than
landing partial sanitisation.

---

## 6. The PII firewall, audit, and cleanup

### 6.1 `ledger_agent/core/privacy.py` (R-46)

Detector-based redactor. Public entry point: `redact(text, allow_pii=False)`.
Detector registry `_ALL_DETECTORS` covers EIN, SSN, account numbers, phone,
email, API keys, allcaps token sequences, etc. Re-opened defects to be
aware of:

- BUG-P2 — claimed `_detect_corpus_names()` and `config/`-driven loader do
  not exist (ARCH-39).
- SMELL-P6 — claimed bounded-lookahead pattern not present; O(n²)
  backtracking risk latent (ARCH-39).
- SMELL-M4 — `mcp/server.py:46-48` still does
  `json.dumps → regex → json.loads` instead of a structural walk
  (ARCH-39).

### 6.2 `ledger_agent/core/audit.py`

Append-only JSONL log at `ledger_agent/data/audit/run-<run_id>.jsonl`,
created with mode `0o600`. Public entry points: `audit(event, **kwargs)`,
`shutdown_audit()` (registered via `atexit`). `_redact_kwargs` coerces
Decimal/bytes/dataclass via `str(v)` before redaction. On redaction failure
the kwargs are replaced with an `[AUDIT_REDACT_UNAVAILABLE]` sentinel —
never the raw value.

### 6.3 `ledger_agent/core/cleanup.py` (R-46 §7)

Sweeps three classes of scratch state at the end of every job and at boot:

- `data/raw_cache/**` — intermediate PDF text / unredacted JSON
- `data/exports/_tmp/**` — half-written exports
- `$TMPDIR/ledger-agent-raw-*` — process-local scratch

The wrapper pattern:

```python
from ledger_agent.core.cleanup import run_cycle, boot_cleanup

boot_cleanup()                          # called once at process start

with run_cycle("import_statements"):    # wraps each job
    ...                                 # cleanup runs on __exit__
```

Long-running work that produces transient files MUST be wrapped in
`with run_cycle(...)`. The hook registry is `register_cleanup_hook(fn)`.

Re-opened defects: BUG-C1 (`_write_lock` / `hold_write_lock` / drain-then-skip
not implemented), SMELL-C4 (uid filter present but
`cleanup.skipped_foreign_uid` audit event not emitted). See ARCH-40.

### 6.4 The MCP egress chain

`ledger_agent/mcp/server.py`:

1. API-key sweep (BUG-M1 fixed — runs unconditionally before the branch).
2. Read `_meta.allow_pii` from the tool-call payload.
3. `call_tool(name, args, allow_pii=...)` — propagates the flag.
4. `_redact_response(raw_dict, allow_pii=...)` — redacts on egress.
5. On redaction failure: return JSON-RPC error `-32000 redaction_failed:
   <cause>`; do NOT leak raw payload. The firewall fails closed.

The bridge (`ledger_agent/bridge/jsonrpc_stdio.py`) honours the same
`_meta.allow_pii` flag and runs `_redact_bridge_response` (note: the
function is named `_redact_bridge_response`, not `_redact_response` — a
historical doc mismatch).

---

## 7. Verification discipline (the most important section)

> **Rule.** Never mark a ticket / acceptance criterion / status row ✅
> without on-disk verification — `ls` for the file, `grep` for the symbol,
> a passing test for the behaviour. If the artefact isn't on disk, the
> work isn't done.

The 2026-05-14 hardening pass re-opened roughly nineteen previously-✅ rows
because the cited code did not exist. The pattern was always the same: a
plausible-sounding function name was named in the doc, the work was claimed
done, but the symbol was nowhere in the repo. The fix is mechanical:

1. Before flipping a status marker, run:
   ```bash
   grep -rn "<claimed_symbol>" ledger_agent/ tests/ scripts/
   ```
   If zero hits, the work is not done.
2. For behaviour claims, run the specific test the acceptance row names.
   If the test file does not exist, the claim is fictional.
3. For file-creation claims, `ls -la` the path. `__pycache__/*.pyc` does
   not count as the file existing (ARCH-19 / ARCH-39 / ARCH-40 / ARCH-41
   were all caught by orphan-bytecode mismatches).
4. When you legitimately complete a row, the commit that closes it should
   touch the row and the artefact in the same change. One without the
   other is the over-claim pattern.

`requirement-and-review-feedback.md` is the canonical board. Do not add new
sections; update rows in place. Do not append a chronological journal — git
history serves that role.

---

## 8. Coding conventions

- **Python version.** Target `py310` (ruff config); code may use 3.10+
  syntax including `|` unions and `match`.
- **Formatter / linter.** `ruff` is the source of truth (`pyproject.toml
  [tool.ruff]`). Run `ruff check .` before committing. No `black` config
  on disk; do not add one.
- **Type hints.** `mypy` is configured (`[tool.mypy]`). New public
  functions should carry type hints. The existing code is partially
  typed; do not block on missing hints in legacy modules.
- **Imports.** `core` purity rule (ARCH-02): code under
  `ledger_agent/core/` MUST NOT import any of `cli`, `rich`, `click`,
  `typer`, `requests`, `httpx`, `fastapi`, `flask`, `questionary`,
  `colorama`. Exceptions are explicitly allowlisted in
  `tests/architecture/test_core_purity.py` (currently `reports/renderer.py`
  for `rich` and `logging_setup.py`).
- **Single source of truth.** All six core operations live in
  `ledger_agent.core.api`. Forms B (CLI), C (MCP), D (webapp) are thin
  wrappers; they MUST NOT reimplement business logic.
- **Plugin registration.** Statement parsers are auto-discovered by
  `pkgutil.iter_modules` over `ledger_agent/core/parsers/`. To add an
  institution, create one file subclassing `BaseStatementParser`,
  decorated with `@ParserRegistry.register`. Do not edit
  `parsers/__init__.py` or registry code.

---

## 9. Test guidance

- `pytest` is the only test runner; markers declared in `pyproject.toml`
  (`parity`, `integration`).
- `tests/conftest.py` sets `FI_DB_PATH` before collection via
  `pytest_configure` so `config.py` reads a per-session tmpdir.
- Per-test DB: use the `fresh_db` fixture (per-test scope) which clones the
  session DB and restores `FI_DB_PATH` on teardown.
- The parity test (`tests/integration/test_2024_cpa_parity.py`) skips
  cleanly when `FI_CPA_CORPUS_PATH` is absent (`SKIP_REASON` constant).
  Do not invent a different skip mechanism for new parity tests — extend
  the existing scaffold.
- Parity tolerance is `Decimal("1.00")`. Any divergence > $1 is P0.
- The brokerage fixture corpus at
  `tests/integration/fixtures/brokerage/<inst>/<period>/{statement.pdf,
  expected.json}` is gitignored at the PDF level (`*.pdf` rule); the
  `expected.json` files are committed. ARCH-24 / ARCH-32 cannot close in
  CI until the corpus is wired (either as a GitHub secret or via the
  `FI_CPA_CORPUS_2024` extraction step).

---

## 10. Commit / PR conventions

- **Conventional commits** drive semver (R-51 / ARCH-11). `feat:` bumps
  minor, `fix:` bumps patch, `feat!:` or `BREAKING CHANGE:` bumps major.
  See `.github/scripts/compute_semver.py`.
- **One commit, one row.** When you close a row in
  `requirement-and-review-feedback.md`, the commit that flips the marker
  should touch the underlying artefact in the same diff. Reviewers (human
  or agent) will reject commits that flip ✅ without an accompanying code
  change.
- **Never `git add -A` or `git add .`.** Stage explicit paths. The
  `.gitignore` is generous but the consequences of a single committed PDF
  or `.env` are bad.
- **Forbidden in any commit:**
  - real entity / partner / bank / broker / ticker names
  - cent-precision dollar figures near financial nouns
  - account numbers (use last-4 only)
  - anything under `data/`, `statements/`, `private/`, `.env*`, `*.pdf`,
    `*.ofx`, `*.qfx`, `*.qbo`, `*.db`, `*.xlsx`, `*.csv`,
    `*_report.json`, `*_context.json`, `*_memory.json`
  - any plan file matching `*.plan.md` or anything under `.wibey/`
- **Branch naming.** Use `arch-NN-<slug>` for tracked ARCH tickets;
  conventional-commit prefix in the first commit message governs semver.
- **Never `--no-verify`.** If the pre-commit scanner blocks you, fix the
  hit. If you believe the hit is a false positive, add `# redaction:
  allow` inline with a comment explaining why, or extend the file-level
  allowlist.

---

## 11. Forbidden patterns

Quick reference. Any of these is grounds for rejecting the change:

1. Flipping a status to ✅ without on-disk verification.
2. Committing a real name, real figure, real account number, or anything
   matching the privacy corpus.
3. Reading raw PII outside an `allow_pii=True` opt-in path.
4. Adding network egress (`requests`, `httpx`, `urllib`) anywhere under
   `ledger_agent/core/`.
5. Importing `cli`, `rich`, `click`, `typer`, `questionary`, `colorama`,
   `fastapi`, or `flask` from `ledger_agent/core/` (architecture purity
   rule, ARCH-02).
6. Reimplementing one of the six core operations in `cli/`, `mcp/`,
   `bridge/`, or `webapp/`. Forms B/C/D MUST be thin wrappers over
   `ledger_agent.core.api`.
7. Touching `private/pseudonym-map.local.md` from agent code.
8. Skipping audit logging on a tool-call exit path. Errors get
   `audit("<form>.tool_error", ...)`; successful redaction skips get
   `audit("<form>.redaction.skipped", reason=...)`.
9. Catching `BaseException` or bare `except:` in any of the privacy /
   audit / cleanup paths.
10. Echoing the offending value into any error message, scanner output,
    or PR comment. The pattern is `path:line:category` — never the
    matched value.

---

## 12. Currently open hard problems

Pointer list. Full bodies in `requirement-and-review-feedback.md` §4.

- **CPA-parity track (§4.1, P0s).** ARCH-24 brokerage parser completeness
  (🚧, blocked on fixtures); ARCH-25 position-table backfill;
  ARCH-26 silent-drop aggregator; ARCH-28 liability recognition;
  ARCH-29 partner-withholding reclassification; ARCH-30 entity isolation;
  ARCH-31 fiscal-year carry-forward; ARCH-32 golden integration test.
  ARCH-27 (persist classification at classify time) is P1 but on the
  critical path for ARCH-29.

- **Doc-privacy track (§4.2 + §4.4).** ARCH-33 corpus + policy (artefacts
  on disk, acceptance partial); ARCH-34 scanner (on disk, tests missing);
  ARCH-35 pre-commit hook (not wired); ARCH-36 CI gate (not wired);
  ARCH-37 sanitise tracked docs (re-opened); ARCH-38 history audit (not
  done); ARCH-41 non-markdown sweep (re-opened, scanner verification
  blocked); **ARCH-43 P0 markdown sweep + parser-module rename** — the
  current unblocker.

- **Runtime privacy residuals (§3.1, ARCH-39).** BUG-P2 (corpus-name
  detector fictional), SMELL-P6 (no bounded lookahead), SMELL-M4
  (structural redact still string-based), BUG-B1 (audit-on-error placement
  inconsistent with claim).

- **Cleanup safety (ARCH-40).** BUG-C1 (write-lock drain-then-skip
  entirely unimplemented), SMELL-C4 (uid filter present but audit event
  missing).

- **Data integrity (ARCH-42).** LTCG COA still uses 5071 (Legal Fees)
  instead of 5075 — `ledger_agent/core/api.py:277`. BANK_X→BROKER_Z
  transfer classification single-key only. BalanceSheet builder has no
  `pl_periods` parameter. `_trading_report.py` scratch script still
  tracked at repo root.

---

## 13. Quick reference card

| Need to... | Look at |
|---|---|
| Pick up a ticket | `requirement-and-review-feedback.md` §2 status board |
| Understand ticket body | `requirement-and-review-feedback.md` §4 |
| Find a code-review finding | `requirement-and-review-feedback.md` §3.1 / §3.2 |
| Understand the architecture | `STRUCTURE.md` (verify paths against disk!) |
| User-facing usage | `README.md` |
| Privacy rules | `docs/redaction-policy.md` |
| Pseudonym corpus | `config/redaction_corpus.yaml` |
| Real-name map | `private/pseudonym-map.local.md` (gitignored, may be absent) |
| Run scanner | `python scripts/check_doc_redaction.py --staged` |
| Run parity tests | `pytest -m parity tests/integration/test_2024_cpa_parity.py` |
| Architecture purity | `pytest tests/architecture/test_core_purity.py` |
| Audit log location | `ledger_agent/data/audit/run-<run_id>.jsonl` |
| Core operations | `ledger_agent.core.api` (six public functions) |
| MCP tool schemas | `ledger_agent/mcp/tools.py` (`TOOL_SCHEMAS`) |
| Bridge JSON-RPC | `ledger_agent/bridge/jsonrpc_stdio.py` |
| Schema version | `ledger_agent/core/database.py` `SCHEMA_VERSION` |

---

*End of file. Treat this as the entry contract — when in doubt, prefer
honesty about what is and is not on disk.*

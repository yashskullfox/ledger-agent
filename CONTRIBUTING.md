# Contributing to ledger-agent

Thank you for your interest in contributing! This document explains how to
set up a development environment, run the test suite, and submit a change.

---

## Privacy contract (read first)

This codebase handles real partnership accounting data. Before you write a
single line, internalise these rules:

1. **No real names in tracked files.** All entity names, partner names, bank
   names, brokerage names, and ticker symbols must use the canonical
   pseudonyms in `config/redaction_corpus.yaml`
   (`ENTITY_A`, `PARTNER_1`, `BANK_X`, `BROKER_Y`, …).
2. **No cent-precision figures.** Dollar amounts with decimal cents
   (`$X,XXX.XX`) trigger the scanner. Use approximate notation (`~$X,XXX`)
   in prose.
3. **Run the scanner before every commit.**

   ```bash
   python scripts/check_doc_redaction.py --staged
   ```

   Exit code 0 = clean. Exit code 1 = at least one hit; fix before staging.

The pre-commit hook (`pre-commit install`) runs the scanner automatically.

---

## Development setup

### Prerequisites

- Python **≥ 3.10** (CI tests 3.10, 3.11, 3.12; release builds pin to 3.11.9)
- JDK **21** (temurin) — only needed to build Form D (Spring Boot fat jar)
- Maven **3.9+** — only needed for `webapp/`

### Install

```bash
git clone https://github.com/your-org/ledger-agent.git
cd ledger-agent

# Runtime + dev dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Optional: reproducible install from the pinned lockfile
# pip install --require-hashes -r requirements.lock
#   ↑ The lockfile embeds an internal PyPI index.
#     Override with --index-url https://pypi.org/simple/ on public networks.

# Copy the env-var template and fill in your values
cp .env.example .env
```

### Configure

`ledger-agent` reads all configuration from environment variables (prefix
`FI_` for Python, `LEDGER_` for the Spring Boot webapp). See
`docs/env-vars.md` for the full reference.

Minimum required for local development:

```bash
# .env
FI_AI_BACKEND=local          # no external API key needed
FI_LOG_LEVEL=INFO
```

### Run the test suite

```bash
pytest                                      # full suite (~344 tests)
pytest -q --tb=short                        # terse output
pytest tests/architecture/ -q               # architecture guardrails only
pytest -m parity -q                         # CPA parity gate (needs corpus)
```

The parity tests skip gracefully when `statements/2024.txt` (gitignored CPA
corpus) is absent — they will not error in a fresh clone.

### Run the redaction scanner

```bash
python scripts/check_doc_redaction.py --all-tracked   # full repo
python scripts/check_doc_redaction.py --staged        # staged files only
python scripts/check_doc_redaction.py --paths FILE…   # specific paths
```

### Build Form D (Spring Boot)

```bash
cd webapp
./mvnw package -DskipITs
java -jar target/ledger-agent-webapp-*.jar
```

---

## Making a change

### Branching

Use the format `type/short-description`:

```
feat/add-cogs-line
fix/wash-sale-rounding
docs/update-contributing
```

### Code style

- **Python**: `ruff` (config in `pyproject.toml`). Run `ruff check .` before
  committing. Line length 100, target Python 3.10.
- **Java**: standard Spring Boot conventions; no additional linter required.
- **Preserve existing patterns.** Do not refactor a file just because you
  are nearby; keep diffs minimal.

### Writing tests

- Test **behaviour**, not implementation details.
- Each test should answer: *"What user-visible outcome would break if this
  test fails?"*
- Avoid tests that only verify a function was called; verify the result.
- Parity tests (`-m parity`) require the private CPA corpus — use `xfail` or
  `skip` for divergences that cannot be fixed without private data.

### Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add COGS separation to generate_form_1065
fix: remove deposit keyword from 4020 classifier
docs: add env-var reference table
test: add wash-sale unit test
chore: bump maven-compiler-plugin to 3.14.0
```

### Scanner gate (CI)

The redaction scanner runs on every push and pull request
(`.github/workflows/redaction-scan.yml`). A failure blocks merge. Fix any
hits before requesting review — the workflow posts per-line annotations to the
PR.

### Pull request checklist

- [ ] `python scripts/check_doc_redaction.py --all-tracked` exits 0
- [ ] `pytest --tb=short -q` exits 0 (or new failures are `xfail`-marked
      with a reason)
- [ ] No real entity names, partner names, institution names, or
      cent-precision figures in any tracked file
- [ ] Commit messages follow the Conventional Commits format

---

## Project structure

```
ledger_agent/core/   # Form A — core engine (api.py is the public surface)
ledger_agent/cli/    # Form B — CLI (app() entry point)
ledger_agent/mcp/    # Form C — MCP server
webapp/              # Form D — Spring Boot fat jar
tests/               # pytest suite
scripts/             # Dev tooling (scanner, corpus regen, runner)
docs/                # Architecture and diagnostic notes
config/              # Pseudonym corpus (redaction_corpus.yaml)
private/             # Gitignored — real institution strings, allowlists
```

See `STRUCTURE.md` and `AGENTS.md` for a more detailed map.

---

## Getting help

- Open an [issue](https://github.com/your-org/ledger-agent/issues) for bugs
  or feature requests using the provided templates.
- For security vulnerabilities, see [SECURITY.md](SECURITY.md).

**Privacy reminder in issues and PRs:** Do not paste real entity names,
partner names, account numbers, institution names, or financial figures into
GitHub issues or PR descriptions. Use the canonical pseudonyms from
`config/redaction_corpus.yaml`.

# Release Process

This document describes the `ledger-agent` release workflow: what gets built,
how versions are computed, which gates must be green, how to cut a release,
and how end users consume each artifact.

The authoritative source is [`.github/workflows/release.yml`](../.github/workflows/release.yml).
If this doc disagrees with the workflow, the workflow wins — please open a PR
to correct this file.

Related reading:

- [`./redaction-policy.md`](./redaction-policy.md) — pseudonym rules and scanner usage
- [`./history-audit.md`](./history-audit.md) — W3/W5 history-squash gate (first public release only)
- [`./parity-divergence.md`](./parity-divergence.md) — known parity-corpus quirks
- [`../requirement-and-review-feedback.md`](../requirement-and-review-feedback.md) — W-lane history (exempt from scanner)

---

## 1. Overview

Each release publishes **four artifact forms** plus per-platform variants of
Form D:

| Form | Artifact                                                | Audience                                  |
|------|---------------------------------------------------------|-------------------------------------------|
| A    | `ledger-agent-core-vX.Y.Z.zip`                          | Library users importing the pure Python core |
| B    | `ledger-agent-cli-vX.Y.Z.tar.gz`                        | Operators running the CLI (`run.sh` / `run.bat`) |
| C    | `ledger-agent-mcp-vX.Y.Z.zip`                           | MCP integrators (stdio + HTTP)            |
| D    | `ledger-agent-webapp-vX.Y.Z-<platform>.jar` × 3         | End users wanting the Spring Boot webapp  |

Form D builds on three runners and produces three jars:

- `ledger-agent-webapp-vX.Y.Z-linux-x86_64.jar`
- `ledger-agent-webapp-vX.Y.Z-darwin-aarch64.jar`
- `ledger-agent-webapp-vX.Y.Z-windows-x86_64.jar`

A `SHA256SUMS` file accompanies the release with checksums of every asset.

**Versioning.** Versions are computed automatically by
[`.github/scripts/compute_semver.py`](../.github/scripts/compute_semver.py)
from conventional commits since the last `vX.Y.Z` tag:

| Commit prefix                                            | Bump  |
|----------------------------------------------------------|-------|
| `feat!:`, `fix!:`, `refactor!:`, `BREAKING CHANGE:`      | major |
| `feat:` / `feat(scope):`                                 | minor |
| `fix:` / `refactor:` / `chore:` / `perf:` / `ci:` / etc. | patch |
| (no conventional prefix found)                           | patch |

If no `vX.Y.Z` tag exists yet, the version starts from `0.1.0`.

**Reproducibility.** `SOURCE_DATE_EPOCH` is exported from the commit timestamp
(`git log -1 --format=%ct`) and propagated to every build job, including the
Maven `project.build.outputTimestamp` for Form D. Two builds from the same
commit produce byte-identical artifacts.

---

## 2. Triggers

Three triggers fire the release workflow:

| Trigger              | When                                          | Publishes? |
|----------------------|-----------------------------------------------|------------|
| `push` to `main`     | Every merge to `main`                         | Yes        |
| `release: created`   | A GitHub Release is created (e.g. via `gh release create vX.Y.Z`) | Yes        |
| `workflow_dispatch`  | Manual run from the Actions UI                | Conditional on `dry_run` input |

For `workflow_dispatch`, the `dry_run` input controls publishing:

- `dry_run: false` (default) → build, test, **and publish**
- `dry_run: true`            → build and test only; **skip** the publish job

Concurrency is scoped per ref (`group: release-${{ github.ref }}`,
`cancel-in-progress: false`) — overlapping runs on the same branch queue
rather than cancel.

---

## 3. Job graph

Ten jobs in total. Dependencies are listed in each row's `needs:`.

```
                       ┌───────────────────┐
                       │  compute-version  │
                       └─────────┬─────────┘
              ┌──────────┬───────┼────────┬──────────┬────────────────┐
              ▼          ▼       ▼        ▼          ▼                ▼
        build-core  build-cli build-mcp build-     build-          build-
                                        webapp-    webapp-         webapp-
                                        linux      macos           windows
              │          │       │        │          │                │
              └────┬─────┴───┬───┘        │          │                │
                   ▼         ▼            │          │                │
              parity-gate  smoke          │          │                │
                   │         │            │          │                │
                   └────┬────┴────────────┴──────────┴────────────────┘
                        ▼
                     release
```

| Job                      | Runner          | Purpose                                                       | `needs:`                                                                     |
|--------------------------|-----------------|---------------------------------------------------------------|------------------------------------------------------------------------------|
| `compute-version`        | ubuntu-latest   | Compute semver + export commit epoch for reproducible builds  | —                                                                            |
| `build-core`             | ubuntu-latest   | Package Form A (core zip); run core-purity test               | `compute-version`                                                            |
| `build-cli`              | ubuntu-latest   | Package Form B (CLI tarball with `run.sh` + `run.bat`)        | `compute-version`                                                            |
| `build-mcp`              | ubuntu-latest   | Package Form C (MCP server zip); run 13 MCP privacy tests     | `compute-version`                                                            |
| `build-webapp-linux`     | ubuntu-latest   | Build Form D fat jar for linux-x86_64                         | `compute-version`                                                            |
| `build-webapp-macos`     | macos-latest    | Build Form D fat jar for darwin-aarch64                       | `compute-version`                                                            |
| `build-webapp-windows`   | windows-latest  | Build Form D fat jar for windows-x86_64                       | `compute-version`                                                            |
| `parity-gate`            | ubuntu-latest   | CPA parity gate (ARCH-12/21/23); optional regen-from-secret   | `build-core`, `build-cli`, `build-mcp`                                       |
| `smoke`                  | ubuntu-latest   | Architecture + MCP privacy smoke tests before publish         | `build-core`, `build-cli`, `build-mcp`                                       |
| `release`                | ubuntu-latest   | Flatten artifacts, generate SHA256SUMS + notes, `gh release`  | `compute-version`, `smoke`, `parity-gate`, `build-webapp-{linux,macos,windows}` |

The `release` job is skipped when `workflow_dispatch.inputs.dry_run == 'true'`.

---

## 4. Pre-release gates

Everything below MUST be green before cutting a release. The workflow enforces
the items marked CI; the rest are operator responsibilities.

### 4.1 Scanner clean (CI on every PR; manual before tagging)

The doc-redaction scanner must return zero hits across all tracked files:

```bash
python scripts/check_doc_redaction.py --strict --all-tracked
```

- Exit `0` → clean, release can proceed.
- Exit `1` → one or more hits; fix per [`./redaction-policy.md`](./redaction-policy.md)
  (pseudonymise, add a `# redaction: allow` annotation, or add an entry to
  `redaction.allowlist` / `config/redaction_corpus.yaml → exempt_files`).
- Exit `2` → configuration error (missing `PyYAML`, missing corpus,
  `--strict` with an empty denylist).

The corpus lives in `config/redaction_corpus.yaml`. Real identifiers live in
the gitignored `private/pseudonym-map.local.md`.

### 4.2 Full test suite (CI)

The CI suite must be green. The Form A/B/C build jobs each run targeted
test slices:

- `build-core` → `tests/architecture/test_core_purity.py`
- `build-cli`  → `python -m ledger_agent.cli.main --help` smoke
- `build-mcp`  → `tests/integration/test_mcp_privacy.py` (13 privacy tests)
                 + `len(TOOL_SCHEMAS) == 6` assertion
- `smoke`      → core purity + MCP privacy combined

Locally, run the full suite — 344+ tests should pass, with documented
`xfail`s. Anything not on the documented xfail list is a release blocker.

### 4.3 CPA parity gate (CI)

`parity-gate` job:

1. Asserts `tests/integration/fixtures/2024_cpa_expected.json` exists and is valid JSON.
2. **If** `FI_CPA_CORPUS_2024` GitHub Actions secret is set (intended for tagged
   releases), regenerates the fixture from the raw text via
   `scripts/regen_parity_corpus.py` and diffs it against the committed JSON,
   ignoring keys that start with `_` (volatile metadata). Any field-level
   drift fails the gate.
3. Runs `pytest -m parity tests/integration/test_2024_cpa_parity.py --maxfail=1`.
   Exit code 5 ("no tests collected") is treated as failure — fixture tests
   must always collect.

### 4.4 History squashed (first public release only)

Per [`./history-audit.md`](./history-audit.md), the current git history
contains denylisted tokens across all 45 commits and 11 local branches.
**Before the first public release**, the owner must squash to an "Initial
commit" in a fresh mirror clone (option (b) in the audit). This gate is
operator-driven; CI does not enforce it.

Subsequent releases do not need to re-squash — only commits authored against
the cleaned history.

### 4.5 Reproducible builds (CI)

`SOURCE_DATE_EPOCH` is anchored to the commit time (`git log -1 --format=%ct`)
in `compute-version` and propagated to all downstream jobs. Form D additionally
sets `project.build.outputTimestamp` in `pom.xml` to the matching UTC ISO
timestamp. Do not introduce build steps that read wall-clock time.

---

## 5. How to cut a release

### 5.1 Local dry run (recommended before pushing)

Validate locally that the scanner, parity gate, and smoke tests are green:

```bash
# 1. Scanner
python scripts/check_doc_redaction.py --strict --all-tracked

# 2. Parity gate
python -m pytest -m parity tests/integration/test_2024_cpa_parity.py -q

# 3. Smoke
python -m pytest tests/architecture/test_core_purity.py tests/integration/test_mcp_privacy.py -q

# 4. (Optional) full suite
python -m pytest -q
```

### 5.2 CI dry run via `workflow_dispatch`

From the GitHub Actions UI (or `gh`), trigger the workflow manually with
`dry_run: true`. All build and gate jobs run; the `release` job is skipped.

```bash
gh workflow run release.yml -f dry_run=true
gh run watch
```

### 5.3 Tagged release path

```bash
# 1. Make sure main is green and your local tree is clean.
git checkout main && git pull --ff-only

# 2. Determine the next version. compute_semver.py prints what CI will compute:
python .github/scripts/compute_semver.py

# 3. Create the GitHub Release. This fires the `release: created` trigger.
gh release create vX.Y.Z --generate-notes
```

The workflow then:

1. Re-computes the version (CI is the source of truth).
2. Builds Forms A–D in parallel.
3. Runs the parity gate and smoke tests.
4. Downloads all build artifacts into `release-assets/`.
5. Generates `SHA256SUMS` via `.github/scripts/sha256sums.sh`.
6. Generates release notes from `git log <last-tag>..HEAD` (`--no-merges`,
   top 30 commits) and uploads all assets to the GitHub Release.

### 5.4 Push-to-main path

A bare `git push origin main` also fires the workflow. CI computes the next
version, builds everything, runs gates, and (if not a dry run) publishes a
release tagged with the computed version. Use this for normal patch/minor
bumps; reserve `gh release create` for releases where you want to pin the
version explicitly or write custom release notes.

### 5.5 What happens after publish

- All artifacts are uploaded to the GitHub Release page.
- `SHA256SUMS` lists checksums for every asset.
- Release notes include a "What's Changed" section (conventional commits
  since the previous tag) and an "Artifacts" table mapping each filename to
  its form letter.

---

## 6. Artifact consumer guide

Each form is independently consumable. Pick the one that matches your use case.

### 6.1 Form A — Core library (`ledger-agent-core-vX.Y.Z.zip`)

Pure-Python core. No CLI, no MCP, no webapp.

```bash
# Option 1: unzip and import
unzip ledger-agent-core-vX.Y.Z.zip -d ledger-agent-core/
cd ledger-agent-core/
pip install --require-hashes -r requirements.lock
python -c "from ledger_agent.core.api import import_statements; print('ok')"

# Option 2: pip install the zip directly (if packaging/core/pyproject.toml ships)
pip install ledger-agent-core-vX.Y.Z.zip
```

The six pinned API functions are `import_statements`, `generate_balance_sheet`,
`generate_form_1065`, `generate_k1`, `pte_estimate`, `reconcile_year`.

### 6.2 Form B — CLI runner (`ledger-agent-cli-vX.Y.Z.tar.gz`)

Self-bootstrapping launcher. `run.sh` / `run.bat` create a venv, install
deps from `requirements.lock`, and dispatch to the CLI.

**Linux / macOS:**

```bash
tar -xzf ledger-agent-cli-vX.Y.Z.tar.gz
cd ledger-agent-cli-vX.Y.Z/
./run.sh scan ./statements/2024
./run.sh balance 2024
./run.sh tax 2024
./run.sh form1065 2024
./run.sh k1 2024 --partner partner_1
./run.sh reconcile 2024
```

**Windows:**

```bat
tar -xzf ledger-agent-cli-vX.Y.Z.tar.gz
cd ledger-agent-cli-vX.Y.Z\
run.bat scan .\statements\2024
run.bat balance 2024
```

Legacy `main.py` subcommands (`mcp`, `context`, `classify`, `setup`, …) are
also routed through `run.sh` / `run.bat`. See the header comments in those
files for the full list. Environment overrides: `FI_STATEMENTS_DIR`,
`FI_DB_PATH`, `FI_AI_BACKEND` (`local | openai | gemini`),
`FI_OPENAI_API_KEY`, `FI_GEMINI_API_KEY`.

### 6.3 Form C — MCP server (`ledger-agent-mcp-vX.Y.Z.zip`)

Bundles the MCP server, core, and bridge layers.

```bash
unzip ledger-agent-mcp-vX.Y.Z.zip -d ledger-agent-mcp/
cd ledger-agent-mcp/
pip install --require-hashes -r requirements.lock

# stdio transport (most MCP clients)
python -m ledger_agent.mcp.server

# HTTP transport (where supported)
python -m ledger_agent.mcp.server --http --port 8765
```

The server exposes six tools (see `ledger_agent.mcp.tools.TOOL_SCHEMAS`).
If `packaging/mcp/README.md` is present in the artifact, it carries the
authoritative client-side configuration snippet.

### 6.4 Form D — Spring Boot webapp (`ledger-agent-webapp-vX.Y.Z-<platform>.jar`)

Standalone fat jar. Requires JDK 21 (Temurin recommended).

```bash
# Linux
java -jar ledger-agent-webapp-vX.Y.Z-linux-x86_64.jar

# macOS (Apple Silicon)
java -jar ledger-agent-webapp-vX.Y.Z-darwin-aarch64.jar

# Windows
java -jar ledger-agent-webapp-vX.Y.Z-windows-x86_64.jar
```

The app binds to port `8080` by default. Override with
`--server.port=9090` or `SERVER_PORT=9090`.

### 6.5 Fifth way — browser-popup launcher (forward link, W11)

For desktop users who want a one-click experience, W11 will ship a launcher
that starts the webapp (or CLI server mode) and opens the default browser
to the local URL. Once W11 lands, the script will live at
`scripts/run_with_browser.py` and ship inside the CLI tarball; this section
will be expanded with the exact command. Until then, prefer Form D + manual
browser open.

---

## 7. Lockfile + supply chain

Python dependencies are pinned by SHA256 in `requirements.lock`, generated
by `pip-compile --generate-hashes` (from `pip-tools`). CI installs with
`pip install --require-hashes -r requirements.lock`, so any hash mismatch
fails the build.

To regenerate the lockfile (after a deliberate dependency change):

```bash
# Install pip-tools in an isolated venv first
pip install pip-tools

# Regenerate, pinning hashes from PyPI
pip-compile --generate-hashes --output-file=requirements.lock requirements.txt

# Review the diff carefully — every line is a security-relevant pin
git diff requirements.lock

# Commit alongside the requirements.txt change in the same PR
git add requirements.txt requirements.lock
git commit -m "build: bump <package> to <version>"
```

For Form D, Maven dependencies are resolved at build time from `pom.xml`.
Reproducibility is achieved via `project.build.outputTimestamp` rather than
hash pinning. Consider adding [Maven Reproducible Builds checksums](https://maven.apache.org/guides/mini/guide-reproducible-builds.html)
as a follow-up if external consumers need to verify byte-equivalence.

---

## 8. Troubleshooting

### 8.1 Parity gate fails

```
CPA parity gate FAILED (exit N) — release blocked.
```

The committed fixture has drifted from the regen output (only checked when
`FI_CPA_CORPUS_2024` is set), or one of the parity tests itself failed.

```bash
# 1. Regenerate locally and inspect the diff
python scripts/regen_parity_corpus.py --out /tmp/regen.json
diff <(python -m json.tool tests/integration/fixtures/2024_cpa_expected.json) \
     <(python -m json.tool /tmp/regen.json)

# 2. If the diff is intentional, commit the new fixture:
python scripts/regen_parity_corpus.py \
    --out tests/integration/fixtures/2024_cpa_expected.json
git add tests/integration/fixtures/2024_cpa_expected.json
git commit -m "test: regenerate CPA parity fixture for 2024 corpus"
```

See [`./parity-divergence.md`](./parity-divergence.md) for known quirks
that affect the fixture.

### 8.2 Scanner fails on PR

```
N hit(s) found
```

For each hit, the scanner prints `<file>:<line>: hit <category>`. Resolve by:

1. **Pseudonymise.** Replace the offending token with the appropriate
   pseudonym from `config/redaction_corpus.yaml → replacement_tokens`
   (`ENTITY_A`, `PARTNER_1`, `BANK_X`, `BROKER_Y`, etc.).
2. **Line-level allow.** Append `# redaction: allow` to the line if the
   token is a substring collision (common English words that happen to
   contain a denylisted substring) or genuinely required.
3. **File-level exempt.** Add the path to `config/redaction_corpus.yaml →
   exempt_files` for verbatim third-party content or audit reports that
   intentionally enumerate denylist tokens (this is how
   `requirement-and-review-feedback.md` and `docs/history-audit.md` are
   handled).
4. **Repo-level allowlist.** Add the token to `redaction.allowlist` only
   if it is a project-wide false positive.

The full policy lives in [`./redaction-policy.md`](./redaction-policy.md).

### 8.3 Lockfile drift

```
ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.
```

Some dependency was added or upgraded without regenerating the lock. Fix:

```bash
pip-compile --generate-hashes --output-file=requirements.lock requirements.txt
git add requirements.lock
git commit -m "build: refresh requirements.lock"
```

If the failure is `Could not find a version that satisfies the requirement…`,
a transitive dependency was yanked from PyPI; rerun `pip-compile` to pick
the next acceptable version and update accordingly.

### 8.4 Webapp build flake (macOS / Windows)

Form D builds occasionally flake on the per-platform runners due to network
issues fetching Maven dependencies. Re-run the failed job from the Actions
UI; if it fails twice, check `~/.m2/settings.xml` and Maven Central status
before assuming a code issue.

### 8.5 `gh release create` fires the workflow but version mismatches

The CI re-computes the semver from conventional commits, so the version
in the release notes table is the CI-computed one, not necessarily the tag
you provided. To pin a specific version, ensure the conventional commits
since the last tag bump to exactly that version before running
`gh release create`.

---

## 9. Release checklist (TL;DR)

```
[ ] git pull --ff-only origin main
[ ] python scripts/check_doc_redaction.py --strict --all-tracked   # 0 hits
[ ] python -m pytest -q                                            # all green
[ ] python -m pytest -m parity tests/integration/test_2024_cpa_parity.py -q
[ ] python .github/scripts/compute_semver.py                       # eyeball next version
[ ] gh workflow run release.yml -f dry_run=true && gh run watch    # CI dry run
[ ] gh release create vX.Y.Z --generate-notes                       # publish
[ ] Verify release page shows 7 assets + SHA256SUMS
```

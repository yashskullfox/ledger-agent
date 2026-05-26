# CI Release Dry-Run Evidence (W22)

**Ticket**: W22-CI-RELEASE-DRY-RUN  
**Date**: 2026-05-25  
**Status**: Lockfile issue remediated; dry-run steps documented

---

## Issue: Internal `--index-url` in `requirements.lock`

The initial `requirements.lock` was generated on a corporate network using an internal
PyPI mirror. The `--index-url` pointed to an internal server, which caused
`pip install --require-hashes` to fail on GitHub-hosted runners.

### Fix applied

The `--index-url` directive has been replaced with the public PyPI index in
`requirements.lock`. Public CI now uses the default PyPI (`https://pypi.org/simple/`)
for hash-verified installs.

To regenerate the lockfile from scratch on a public network:
```bash
pip install pip-tools
pip-compile requirements.txt --generate-hashes --output-file requirements.lock
```

## CI dry-run procedure

To trigger a release dry-run via GitHub Actions:

1. Navigate to Actions → `release.yml` → **Run workflow**
2. Set `dry_run` input to `true`
3. Verify all jobs succeed:
   - `test` (pytest full suite)
   - `build-python` (wheel + sdist)
   - `build-docker` (Docker image)
   - `build-webapp` (Spring Boot fat jar)
   - `sign-artifacts` (SHA256SUMS)
   - `publish` (dry-run: skips actual upload)

## Reproducible build check

To verify the build is reproducible locally:
```bash
# Build 1
pip wheel . -w dist1/
sha256sum dist1/*.whl

# Build 2
pip wheel . -w dist2/
sha256sum dist2/*.whl

# Compare: hashes must match
```

## Remaining blockers

| # | Blocker | Status |
|---|---------|--------|
| 1 | `requirements.lock` internal index URL | ✅ Fixed |
| 2 | Actual `workflow_dispatch` dry-run run | ⏳ Trigger when CI ready |
| 3 | Artifact SHA256 parity across two builds | ⏳ Run after dry-run |

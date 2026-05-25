## Summary

<!-- Describe your change in 1–3 sentences. No real names or figures. -->

## Checklist

- [ ] `python scripts/check_doc_redaction.py --all-tracked` exits 0
- [ ] `pytest -q --tb=short` exits 0 (new failures marked `xfail` with reason)
- [ ] No real entity names, partner names, institution names, or cent-precision figures in tracked files
- [ ] If `ledger_agent/core/` was changed: `pytest tests/architecture/test_core_purity.py` passes
- [ ] Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`)
- [ ] New public functions have docstrings

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactoring
- [ ] Documentation
- [ ] Tests only
- [ ] CI/tooling

## Testing

<!-- How did you test this change? Which test files were added or modified? -->

## Privacy impact

<!-- Does this change affect PII handling? If yes, describe the safeguards. -->

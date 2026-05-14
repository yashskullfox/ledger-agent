# Makefile  –  Developer convenience targets
# ==========================================

.PHONY: install-hooks check-docs test

install-hooks:
	@echo "Installing git hooks..."
	cp hooks/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "Done. Pre-commit hook installed at .git/hooks/pre-commit"

check-docs:
	python scripts/check_doc_redaction.py --all-tracked -v

test:
	python -m pytest

test-unit:
	python -m pytest tests/unit/ tests/test_*.py -v

test-parity:
	python -m pytest -m parity tests/integration/test_2024_cpa_parity.py --maxfail=1 -q

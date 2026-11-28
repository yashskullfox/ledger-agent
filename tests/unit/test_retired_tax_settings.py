"""
tests/unit/test_retired_tax_settings.py  –  Card Q-06: individual-tax settings are retired

After Q-04 / Q-02 the tax estimate is the entity-level state PTET only, so the individual
self-employment / federal / QBI settings were dead. This module guards that they stay gone
from the engine and that ``FI_STATE_TAX_RATE`` remains the one rate that drives
``TaxEstimator.estimate_entity_ptet``.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Built from parts so this file itself never spells a retired name as a single token.
_RETIRED_ENV_VARS = tuple("FI_" + n for n in ("SE_TAX_RATE", "FED_INCOME_RATE", "QBI_DEDUCTION"))
_RETIRED_CONSTANTS = ("SE_TAX_RATE", "FEDERAL_INCOME_RATE", "QBI_DEDUCTION_RATE")

# Engine code plus the legacy wrappers that could still read settings.
_SCAN_ROOTS = ("ledger_agent", "adapters", "mcp_server")
_SCAN_FILES = ("main.py", "config.py")


def _source_files() -> list[Path]:
    files = [ROOT / f for f in _SCAN_FILES if (ROOT / f).is_file()]
    for root in _SCAN_ROOTS:
        base = ROOT / root
        if base.is_dir():
            files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def test_retired_env_vars_not_read_anywhere_in_source():
    pattern = re.compile(r"\b(" + "|".join(_RETIRED_ENV_VARS) + r")\b")
    hits = [
        f"{path.relative_to(ROOT)}:{lineno}"
        for path in _source_files()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == [], f"retired individual-tax env vars still referenced: {hits}"


def test_retired_constants_removed_from_tax_estimator():
    import ledger_agent.core.accounting.tax_estimator as tax_estimator

    for name in _RETIRED_CONSTANTS:
        assert not hasattr(tax_estimator, name), f"{name} should have been removed (Q-06)"


def test_state_tax_rate_env_var_drives_entity_ptet():
    """FI_STATE_TAX_RATE is read at import time, so check it in a fresh interpreter."""
    code = (
        "from decimal import Decimal\n"
        "from ledger_agent.core.accounting.tax_estimator import TaxEstimator\n"
        "est = TaxEstimator('TEST LLC', 2025).estimate_entity_ptet(Decimal('10000'))\n"
        "print(est.total_annual_tax)\n"
    )
    env = {**os.environ, "FI_STATE_TAX_RATE": "0.07"}
    for key in _RETIRED_ENV_VARS:  # setting them must not matter
        env[key] = "0.99"
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "700.00"

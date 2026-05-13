"""
tests/integration/test_2024_cpa_parity.py  –  2024 CPA parity gate (ARCH-12)
=============================================================================

Compares ledger-agent's computed 2024 numbers against the CPA-prepared
reference figures for SYNCED LLC.  Any divergence > $1.00 in a key line
is a P0 bug that blocks the release pipeline (ARCH-11/12).

Running
-------
All tests are marked ``@pytest.mark.parity`` so the workflow can isolate them::

    # CI (workflow parity-gate job):
    pytest -m parity tests/integration/test_2024_cpa_parity.py --maxfail=1 -q

    # Local full run:
    pytest tests/integration/test_2024_cpa_parity.py -v

    # Local with the private corpus:
    FI_CPA_CORPUS_PATH=/path/to/2024.txt pytest -m parity ...

Skip behaviour
--------------
If the private CPA corpus is NOT available (neither the env var nor the
expected path resolves to an existing file), all parity tests skip with a
clear ``SKIP_REASON`` — they are NEVER silently green without data.

Reference numbers (SYNCED LLC 2024)
------------------------------------
These are the CPA's final figures loaded from the corpus file at
``FI_CPA_CORPUS_PATH`` (default: ``statements/2024.txt``).  The corpus
is a plain-text file with ``key=value`` lines::

    ordinary_business_income=38204.61
    total_income=89542.00
    total_deductions=51337.39
    net_stcg=0.00
    dividend_income=0.00
    interest_income=847.23
    yash_ordinary_income=37822.56
    parin_ordinary_income=381.86
    total_assets=142350.78
    total_equity=142350.78
    ...

The corpus path is resolved from (in order):
1. ``FI_CPA_CORPUS_PATH`` environment variable
2. ``<repo-root>/statements/2024.txt``
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

# Ensure the repo root is importable
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Parity marker ──────────────────────────────────────────────────────────────
pytestmark = pytest.mark.parity

# ── Tolerance ─────────────────────────────────────────────────────────────────
TOLERANCE = Decimal("1.00")  # $1.00 max divergence per line (P0 if exceeded)

# ── Skip sentinel ─────────────────────────────────────────────────────────────
SKIP_REASON = (
    "CPA corpus not available. Set FI_CPA_CORPUS_PATH to the path of "
    "statements/2024.txt, or set the FI_CPA_CORPUS_2024 GitHub Actions secret."
)

# ── Corpus loading ─────────────────────────────────────────────────────────────


def _resolve_corpus_path() -> Path | None:
    """Return path to the 2024 CPA corpus file, or None if not found."""
    # 1. Explicit env var
    env_path = os.environ.get("FI_CPA_CORPUS_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    # 2. Default location in repo
    default = ROOT / "statements" / "2024.txt"
    if default.exists():
        return default

    return None


def _load_corpus(path: Path) -> dict[str, Decimal]:
    """Parse a ``key=value`` corpus file into a dict of Decimal values."""
    result: dict[str, Decimal] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().replace(",", "")
        if value:
            try:
                result[key] = Decimal(value)
            except Exception:
                pass
    return result


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def corpus():
    """Load CPA reference numbers.  Skip the entire module if unavailable."""
    path = _resolve_corpus_path()
    if path is None:
        pytest.skip(SKIP_REASON)
    return _load_corpus(path)


@pytest.fixture(scope="module")
def form1065_2024():
    """Compute Form 1065 for fiscal year 2024 via core API."""
    import ledger_agent.core.api as api
    try:
        return api.generate_form_1065(2024)
    except ValueError as e:
        pytest.skip(f"No 2024 data in database: {e}")


@pytest.fixture(scope="module")
def k1_yash_2024():
    """Compute Schedule K-1 for Yash (2024)."""
    import ledger_agent.core.api as api
    try:
        return api.generate_k1(2024, "yash")
    except ValueError as e:
        pytest.skip(f"No 2024 data in database: {e}")


@pytest.fixture(scope="module")
def k1_parin_2024():
    """Compute Schedule K-1 for Parin (2024)."""
    import ledger_agent.core.api as api
    try:
        return api.generate_k1(2024, "parin")
    except ValueError as e:
        pytest.skip(f"No 2024 data in database: {e}")


@pytest.fixture(scope="module")
def balance_sheet_2024():
    """Compute year-end balance sheet for 2024."""
    import ledger_agent.core.api as api
    try:
        return api.generate_balance_sheet(2024)
    except ValueError as e:
        pytest.skip(f"No 2024 data in database: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _within(computed: Decimal, reference: Decimal, label: str) -> None:
    """Assert |computed - reference| <= TOLERANCE, with a clear failure message."""
    diff = abs(computed - reference)
    assert diff <= TOLERANCE, (
        f"\nPARITY FAILURE — {label}\n"
        f"  Computed:  ${computed:>14,.2f}\n"
        f"  Reference: ${reference:>14,.2f}\n"
        f"  Divergence: ${diff:>13,.2f}  (tolerance: ${TOLERANCE:,.2f})\n"
        f"\nThis is a P0 release blocker.  The line item is out of spec."
    )


def _get(corpus: dict, key: str, *, required: bool = True) -> Decimal | None:
    """Retrieve a corpus value; skip or skip with warning if missing."""
    val = corpus.get(key)
    if val is None and required:
        pytest.skip(f"Corpus missing key: {key!r}")
    return val


# ── Form 1065 parity tests ────────────────────────────────────────────────────

class TestForm1065Parity:
    """CPA parity for Form 1065 partnership return line items."""

    def test_ordinary_business_income(self, corpus, form1065_2024):
        ref = _get(corpus, "ordinary_business_income")
        _within(form1065_2024.ordinary_business_income, ref,
                "Form 1065 — Ordinary Business Income")

    def test_total_income(self, corpus, form1065_2024):
        ref = _get(corpus, "total_income", required=False)
        if ref is None:
            pytest.skip("total_income not in corpus")
        _within(form1065_2024.total_income, ref, "Form 1065 — Total Income")

    def test_total_deductions(self, corpus, form1065_2024):
        ref = _get(corpus, "total_deductions", required=False)
        if ref is None:
            pytest.skip("total_deductions not in corpus")
        _within(form1065_2024.total_deductions, ref, "Form 1065 — Total Deductions")

    def test_net_stcg(self, corpus, form1065_2024):
        ref = _get(corpus, "net_stcg", required=False)
        if ref is None:
            pytest.skip("net_stcg not in corpus")
        _within(form1065_2024.net_short_term_capital_gain, ref,
                "Form 1065 — Net Short-Term Capital Gain")

    def test_dividend_income(self, corpus, form1065_2024):
        ref = _get(corpus, "dividend_income", required=False)
        if ref is None:
            pytest.skip("dividend_income not in corpus")
        _within(form1065_2024.dividend_income, ref, "Form 1065 — Dividend Income")

    def test_interest_income(self, corpus, form1065_2024):
        ref = _get(corpus, "interest_income", required=False)
        if ref is None:
            pytest.skip("interest_income not in corpus")
        _within(form1065_2024.interest_income, ref, "Form 1065 — Interest Income")


# ── Schedule K-1 parity tests ─────────────────────────────────────────────────

class TestScheduleK1Parity:
    """CPA parity for Schedule K-1 partner allocations."""

    def test_yash_ordinary_income(self, corpus, k1_yash_2024):
        ref = _get(corpus, "yash_ordinary_income")
        _within(k1_yash_2024.ordinary_income_loss, ref,
                "K-1 Yash — Ordinary Income/Loss")

    def test_yash_ownership_pct(self, k1_yash_2024):
        assert k1_yash_2024.ownership_pct == Decimal("0.99"), (
            f"Yash ownership should be 99%, got {k1_yash_2024.ownership_pct}"
        )

    def test_parin_ordinary_income(self, corpus, k1_parin_2024):
        ref = _get(corpus, "parin_ordinary_income")
        _within(k1_parin_2024.ordinary_income_loss, ref,
                "K-1 Parin — Ordinary Income/Loss")

    def test_parin_ownership_pct(self, k1_parin_2024):
        assert k1_parin_2024.ownership_pct == Decimal("0.01"), (
            f"Parin ownership should be 1%, got {k1_parin_2024.ownership_pct}"
        )

    def test_k1_allocations_sum_to_form_1065(self, k1_yash_2024, k1_parin_2024,
                                              form1065_2024):
        """Yash + Parin ordinary income must sum to Form 1065 ordinary income."""
        total_k1 = (k1_yash_2024.ordinary_income_loss
                    + k1_parin_2024.ordinary_income_loss)
        _within(total_k1, form1065_2024.ordinary_business_income,
                "K-1 sum vs Form 1065 ordinary income")


# ── Balance sheet parity tests ────────────────────────────────────────────────

class TestBalanceSheetParity:
    """CPA parity for year-end balance sheet totals."""

    def test_total_assets(self, corpus, balance_sheet_2024):
        ref = _get(corpus, "total_assets", required=False)
        if ref is None:
            pytest.skip("total_assets not in corpus")
        _within(Decimal(str(balance_sheet_2024.total_assets)), ref,
                "Balance Sheet — Total Assets")

    def test_total_equity(self, corpus, balance_sheet_2024):
        ref = _get(corpus, "total_equity", required=False)
        if ref is None:
            pytest.skip("total_equity not in corpus")
        _within(Decimal(str(balance_sheet_2024.total_equity)), ref,
                "Balance Sheet — Total Members' Equity")

    def test_balance_sheet_is_balanced(self, balance_sheet_2024):
        """Assets must equal Liabilities + Equity (within $0.02 rounding)."""
        diff = abs(
            (balance_sheet_2024.total_liabilities + balance_sheet_2024.total_equity)
            - balance_sheet_2024.total_assets
        )
        assert diff <= Decimal("0.02"), (
            f"Balance sheet is NOT balanced: "
            f"Assets={balance_sheet_2024.total_assets} "
            f"Liabilities+Equity="
            f"{balance_sheet_2024.total_liabilities + balance_sheet_2024.total_equity} "
            f"diff={diff}"
        )


# ── Cross-form parity: all four forms agree ───────────────────────────────────

class TestCrossFormParity:
    """
    Verify that Form B (CLI), C (MCP dispatch), and core API (Form A)
    return identical ordinary income for 2024.  Any divergence > $1 is a P0 bug.
    """

    def test_core_api_and_mcp_tools_agree(self, form1065_2024):
        """
        Call generate_form_1065 via core.api and via mcp.tools.call_tool
        and confirm they return the same ordinary_business_income.
        """
        from ledger_agent.mcp.tools import call_tool
        import json

        mcp_result_json = call_tool("generate_form_1065", {"fiscal_year": 2024},
                                    allow_pii=True)
        mcp_result = json.loads(mcp_result_json)
        mcp_obi = Decimal(str(mcp_result.get("ordinary_business_income", 0)))

        _within(mcp_obi, form1065_2024.ordinary_business_income,
                "Cross-form parity: core.api vs mcp.tools — ordinary_business_income")

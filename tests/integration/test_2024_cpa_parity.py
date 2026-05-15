"""
tests/integration/test_2024_cpa_parity.py  –  2024 CPA parity gate (ARCH-12 / ARCH-21)
========================================================================================

Compares ledger-agent's computed 2024 numbers against the CPA-prepared
reference figures for SYNCED LLC.  Any divergence > $1.00 in a key line
is a P0 bug that blocks the release pipeline (ARCH-11/12/21).

Running
-------
All tests are marked ``@pytest.mark.parity`` so the workflow can isolate them::

    # CI (workflow parity-gate job — uses committed JSON fixture):
    pytest -m parity tests/integration/test_2024_cpa_parity.py --maxfail=1 -q

    # Local full run:
    pytest tests/integration/test_2024_cpa_parity.py -v

    # Local — regenerate fixture from raw CPA file first:
    python scripts/regen_parity_corpus.py
    pytest -m parity tests/integration/test_2024_cpa_parity.py -q

    # Local — skip DB tests but verify parity figures only:
    FI_CPA_CORPUS_PATH=statements/2024/2024.txt pytest -m parity ...

Reference numbers (SYNCED LLC 2024)
------------------------------------
These come from ``tests/integration/fixtures/2024_cpa_expected.json`` which is
the single source of truth authored from the CPA file at
``statements/2024/2024.txt``.  Regenerate via::

    python scripts/regen_parity_corpus.py

Key figures (ARCH-21 / CRIT-03 fixed):
    ordinary_business_income = 18732.00   (Gross Profit $25,101 - Deductions $6,369)
    total_income              = 28101.00   (Gross Receipts)
    total_deductions          = 6369.00
    net_stcg                  = 6042.00
    dividend_income           = 37.00
    yash_ordinary_income      = 18732.00  (100% P&L allocation)
    parin_ordinary_income     = 0.00      (0% P&L allocation)
    total_assets_eoy          = 30139.00  ($573 cash + $29,566 Fidelity)
    total_equity_eoy          = 29852.00  (Partners' Capital)

Skip behaviour
--------------
If neither the JSON fixture nor the raw CPA corpus is available, all parity
tests skip with a clear ``SKIP_REASON`` — they are NEVER silently green without
data.
"""
from __future__ import annotations

import json
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

# ── Paths ─────────────────────────────────────────────────────────────────────
FIXTURE_PATH = ROOT / "tests" / "integration" / "fixtures" / "2024_cpa_expected.json"

# Raw corpus is optional — used only by regen_parity_corpus.py and the CI
# full-regen job.  The committed JSON fixture is the canonical artifact in CI.
_DEFAULT_RAW_CORPUS = ROOT / "statements" / "2024" / "2024.txt"

# ── Skip sentinel ─────────────────────────────────────────────────────────────
SKIP_REASON = (
    "CPA reference fixture not available. Either:\n"
    "  1. Run `python scripts/regen_parity_corpus.py` (requires statements/2024/2024.txt), or\n"
    "  2. Set FI_CPA_CORPUS_PATH to the path of the CPA 2024 reference file.\n"
    "Never silently green without data."
)


# ── Corpus loading ─────────────────────────────────────────────────────────────

def _load_fixture() -> dict[str, Decimal] | None:
    """Load from the committed JSON fixture (primary path — works in CI)."""
    if not FIXTURE_PATH.exists():
        return None
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    result: dict[str, Decimal] = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        try:
            result[k] = Decimal(str(v))
        except Exception:
            pass
    return result


def _load_raw_corpus() -> dict[str, Decimal] | None:
    """
    Load from the raw CPA markdown file (fallback when fixture not committed).
    Tries FI_CPA_CORPUS_PATH env var, then the default location.
    """
    env_path = os.environ.get("FI_CPA_CORPUS_PATH")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(_DEFAULT_RAW_CORPUS)

    for p in candidates:
        if p.exists():
            # Delegate to the regen script's parser
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                from regen_parity_corpus import parse_corpus  # type: ignore[import]
                data = parse_corpus(p.read_text(encoding="utf-8"))
                result: dict[str, Decimal] = {}
                for k, v in data.items():
                    if k.startswith("_"):
                        continue
                    try:
                        result[k] = Decimal(str(v))
                    except Exception:
                        pass
                return result
            except Exception:
                pass
    return None


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def corpus():
    """
    Load CPA reference numbers.  Skip the entire module if unavailable.

    Resolution order:
    1. Committed JSON fixture (tests/integration/fixtures/2024_cpa_expected.json)
    2. Raw CPA markdown file ($FI_CPA_CORPUS_PATH or statements/2024/2024.txt)
    """
    data = _load_fixture()
    if data is None:
        data = _load_raw_corpus()
    if data is None:
        pytest.skip(SKIP_REASON)
    return data


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
        f"\nThis is a P0 release blocker.  The line item is out of spec.\n"
        f"Source: {FIXTURE_PATH}"
    )


def _get(corpus: dict, key: str, *, required: bool = True) -> Decimal | None:
    """Retrieve a corpus value; skip if missing."""
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
    """CPA parity for Schedule K-1 partner allocations (ARCH-19 / CRIT-03 fixed)."""

    def test_yash_ordinary_income(self, corpus, k1_yash_2024):
        ref = _get(corpus, "partner_1_ordinary_income")
        _within(k1_yash_2024.ordinary_income_loss, ref,
                "K-1 Yash — Ordinary Income/Loss")

    def test_yash_capital_pct(self, k1_yash_2024):
        """Yash holds 99% of capital (K-1 Part II J — capital)."""
        assert k1_yash_2024.capital_pct == Decimal("0.99"), (
            f"Yash capital_pct should be 99%, got {k1_yash_2024.capital_pct}"
        )

    def test_yash_profit_loss_pct(self, k1_yash_2024):
        """Yash receives 100% of P&L (K-1 Part II J — profit/loss — CRIT-03 fixed)."""
        assert k1_yash_2024.profit_loss_pct == Decimal("1.00"), (
            f"Yash profit_loss_pct should be 100%, got {k1_yash_2024.profit_loss_pct}"
        )

    def test_parin_ordinary_income(self, corpus, k1_parin_2024):
        ref = _get(corpus, "partner_2_ordinary_income")
        _within(k1_parin_2024.ordinary_income_loss, ref,
                "K-1 Parin — Ordinary Income/Loss")

    def test_parin_capital_pct(self, k1_parin_2024):
        """Parin holds 1% of capital (K-1 Part II J — capital)."""
        assert k1_parin_2024.capital_pct == Decimal("0.01"), (
            f"Parin capital_pct should be 1%, got {k1_parin_2024.capital_pct}"
        )

    def test_parin_profit_loss_pct(self, k1_parin_2024):
        """Parin receives 0% of P&L (K-1 Part II J — profit/loss — CRIT-03 fixed)."""
        assert k1_parin_2024.profit_loss_pct == Decimal("0.00"), (
            f"Parin profit_loss_pct should be 0%, got {k1_parin_2024.profit_loss_pct}"
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
        # Corpus uses total_assets_eoy (ARCH-21 key name)
        ref = _get(corpus, "total_assets_eoy", required=False)
        if ref is None:
            ref = _get(corpus, "total_assets", required=False)
        if ref is None:
            pytest.skip("total_assets_eoy not in corpus")
        _within(Decimal(str(balance_sheet_2024.total_assets)), ref,
                "Balance Sheet — Total Assets (EOY)")

    def test_total_equity(self, corpus, balance_sheet_2024):
        # Corpus uses total_equity_eoy (ARCH-21 key name)
        ref = _get(corpus, "total_equity_eoy", required=False)
        if ref is None:
            ref = _get(corpus, "total_equity", required=False)
        if ref is None:
            pytest.skip("total_equity_eoy not in corpus")
        _within(Decimal(str(balance_sheet_2024.total_equity)), ref,
                "Balance Sheet — Total Members' Equity (EOY)")

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


# ── Fixture self-check ────────────────────────────────────────────────────────

class TestFixtureIntegrity:
    """Sanity checks that the JSON fixture is internally consistent."""

    def test_fixture_exists(self):
        """The committed JSON fixture must exist so CI never silently skips."""
        assert FIXTURE_PATH.exists(), (
            f"JSON fixture missing: {FIXTURE_PATH}\n"
            f"Run `python scripts/regen_parity_corpus.py` to regenerate it."
        )

    def test_fixture_is_valid_json(self):
        """JSON fixture must parse cleanly."""
        data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_fixture_ordinary_income_matches_gross_profit_minus_deductions(self, corpus):
        """ordinary_business_income == gross_profit - total_deductions (Form 1065 identity)."""
        gross  = _get(corpus, "gross_profit",            required=False)
        ded    = _get(corpus, "total_deductions",        required=False)
        obi    = _get(corpus, "ordinary_business_income", required=False)
        if gross is None or ded is None or obi is None:
            pytest.skip("gross_profit, total_deductions, or ordinary_business_income missing from fixture")
        assert abs((gross - ded) - obi) <= Decimal("0.01"), (
            f"Fixture integrity error: gross_profit({gross}) - total_deductions({ded}) "
            f"= {gross - ded}, expected ordinary_business_income={obi}"
        )

    def test_fixture_yash_ordinary_income_is_100pct_of_obi(self, corpus):
        """Yash K-1 ordinary income must equal 100% of Form 1065 ordinary income."""
        obi      = _get(corpus, "ordinary_business_income",    required=False)
        yash_oi  = _get(corpus, "partner_1_ordinary_income",   required=False)
        if obi is None or yash_oi is None:
            pytest.skip("ordinary_business_income or partner_1_ordinary_income missing from fixture")
        assert abs(yash_oi - obi) <= Decimal("0.01"), (
            f"Fixture error: partner_1_ordinary_income({yash_oi}) should equal OBI({obi}) "
            f"(100% P&L per CRIT-03 fix)"
        )

    def test_fixture_parin_ordinary_income_is_zero(self, corpus):
        """Parin K-1 ordinary income must be $0 (0% P&L per CRIT-03 fix)."""
        parin_oi = _get(corpus, "partner_2_ordinary_income", required=False)
        if parin_oi is None:
            pytest.skip("partner_2_ordinary_income missing from fixture")
        assert parin_oi == Decimal("0.00"), (
            f"Fixture error: partner_2_ordinary_income should be 0.00 (0% P&L), got {parin_oi}"
        )

    def test_fixture_balance_sheet_identity(self, corpus):
        """Assets == Liabilities + Equity in the CPA fixture (within $1 rounding)."""
        assets  = _get(corpus, "total_assets_eoy",      required=False)
        liab    = _get(corpus, "total_liabilities_eoy", required=False)
        equity  = _get(corpus, "total_equity_eoy",      required=False)
        if assets is None or liab is None or equity is None:
            pytest.skip("Balance sheet corpus keys missing")
        diff = abs(assets - (liab + equity))
        assert diff <= Decimal("1.00"), (
            f"Fixture balance sheet not balanced: "
            f"assets={assets}, liab+equity={liab+equity}, diff={diff}"
        )


# ── Cross-form parity: core API == MCP tools ──────────────────────────────────

class TestCrossFormParity:
    """
    Verify that core API (Form A) and MCP tools dispatch (Form C) return
    identical ordinary income.  Any divergence > $1 is a P0 bug.
    """

    def test_core_api_and_mcp_tools_agree(self, form1065_2024):
        """
        Call generate_form_1065 via core.api and via mcp.tools.call_tool
        and confirm they return the same ordinary_business_income.
        """
        from ledger_agent.mcp.tools import call_tool
        import json as _json

        mcp_result_json = call_tool("generate_form_1065", {"fiscal_year": 2024},
                                    allow_pii=True)
        mcp_result = _json.loads(mcp_result_json)
        mcp_obi = Decimal(str(mcp_result.get("ordinary_business_income", 0)))

        _within(mcp_obi, form1065_2024.ordinary_business_income,
                "Cross-form parity: core.api vs mcp.tools — ordinary_business_income")

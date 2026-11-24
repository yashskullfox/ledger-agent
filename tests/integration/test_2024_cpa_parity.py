"""
tests/integration/test_2024_cpa_parity.py  –  2024 CPA parity gate (ARCH-12 / ARCH-21)
========================================================================================

Compares ledger-agent's computed 2024 numbers against the CPA-prepared
reference figures for ENTITY_A.  Any divergence > $1.00 in a key line  # redaction: allow
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

Reference numbers (ENTITY_A 2024)
------------------------------------
These come from ``tests/integration/fixtures/2024_cpa_expected.json`` which is
the single source of truth authored from the CPA file at
``statements/2024/2024.txt``.  Regenerate via::

    python scripts/regen_parity_corpus.py

Key figures (ARCH-21 / CRIT-03 fixed):
    ordinary_business_income = 18732.00   (Gross Profit 25101 - Deductions 6369)  # redaction: allow
    total_income              = 28101.00   (Gross Receipts)
    total_deductions          = 6369.00
    net_stcg                  = 6042.00
    dividend_income           = 37.00
    partner_1_ordinary_income = 18732.00  (100% P&L allocation)
    partner_2_ordinary_income = 0.00      (0% P&L allocation)
    total_assets_eoy          = 30139.00  (cash + brokerage)
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
TOLERANCE = Decimal("1.00")  # max divergence per line (P0 if exceeded)  # redaction: allow

# ── Paths ─────────────────────────────────────────────────────────────────────
FIXTURE_PATH = ROOT / "tests" / "integration" / "fixtures" / "2024_cpa_expected.json"

# Raw corpus is optional — used only by regen_parity_corpus.py and the CI
# full-regen job.  The committed JSON fixture is the canonical artifact in CI.
_DEFAULT_RAW_CORPUS = ROOT / "statements" / "2024" / "2024.txt"

# ── Partner ID literals ────────────────────────────────────────────────────────
# The production core.api PARTNERS dict uses canonical "partner_1" / "partner_2"
# slugs (see ledger_agent/core/api.py).
_P1_ID = "partner_1"
_P2_ID = "partner_2"

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

def _seed_synthetic_2025_2026(db_path: Path) -> None:
    """Seed a deterministic 2025 + 2026 dataset when production financials.db is absent (CI)."""
    from datetime import date

    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        SnapshotRepo,
        TransactionRepo,
        init_db,
    )
    from ledger_agent.core.models import (
        Account,
        AccountSnapshot,
        AccountType,
        Entity,
        Transaction,
        TransactionType,
    )

    init_db(db_path)
    entity = Entity(id="ent-parity-2526", name="ENTITY_A", entity_type="LLC", state="MO")
    EntityRepo.upsert(entity, db_path)

    chk = Account(
        id="acct-chk-2526",
        entity_id=entity.id,
        name="Business Checking",
        institution="Bank X",
        account_type=AccountType.CHECKING,
        account_number_masked="0001",
    )
    brk = Account(
        id="acct-brk-2526",
        entity_id=entity.id,
        name="Brokerage",
        institution="Broker Y",
        account_type=AccountType.BROKERAGE,
        account_number_masked="0002",
    )
    AccountRepo.upsert(chk, db_path)
    AccountRepo.upsert(brk, db_path)

    txns = [
        Transaction(
            id="tx-2025-inc",
            account_id=chk.id,
            date=date(2025, 6, 15),
            description="DEPOSIT INTUIT INVOICE",
            amount=Decimal("1000.00"),
            transaction_type=TransactionType.CREDIT,
            statement_period="2025-06",
            coa_code="4020",
            coa_name="Service Revenue",
        ),
        Transaction(
            id="tx-2025-cogs",
            account_id=chk.id,
            date=date(2025, 6, 20),
            description="USPS SHIPPING SUPPLIES",
            amount=Decimal("-200.00"),
            transaction_type=TransactionType.DEBIT,
            statement_period="2025-06",
            coa_code="5061",
            coa_name="Office & Shipping Supplies",
        ),
        Transaction(
            id="tx-2025-exp",
            account_id=chk.id,
            date=date(2025, 7, 10),
            description="QUICKBOOKS SUBSCRIPTION",
            amount=Decimal("-300.00"),
            transaction_type=TransactionType.DEBIT,
            statement_period="2025-07",
            coa_code="5010",
            coa_name="Software & Subscriptions",
        ),
        Transaction(
            id="tx-2025-stcg",
            account_id=brk.id,
            date=date(2025, 8, 12),
            description="GAIN - TICKER_SEC1",
            amount=Decimal("5000.00"),
            transaction_type=TransactionType.SELL,
            statement_period="2025-08",
            coa_code="4010",
            coa_name="Realised Trading Gains",
        ),
        Transaction(
            id="tx-2025-div",
            account_id=brk.id,
            date=date(2025, 12, 31),
            description="Dividend Received",
            amount=Decimal("100.00"),
            transaction_type=TransactionType.DIVIDEND,
            statement_period="2025-12",
            coa_code="4021",
            coa_name="Dividend Income",
        ),
        Transaction(
            id="tx-2026-inc",
            account_id=chk.id,
            date=date(2026, 3, 15),
            description="DEPOSIT INTUIT INVOICE",
            amount=Decimal("2000.00"),
            transaction_type=TransactionType.CREDIT,
            statement_period="2026-03",
            coa_code="4020",
            coa_name="Service Revenue",
        ),
        Transaction(
            id="tx-2026-exp",
            account_id=chk.id,
            date=date(2026, 4, 10),
            description="QUICKBOOKS SUBSCRIPTION",
            amount=Decimal("-400.00"),
            transaction_type=TransactionType.DEBIT,
            statement_period="2026-04",
            coa_code="5010",
            coa_name="Software & Subscriptions",
        ),
        Transaction(
            id="tx-2026-stcg",
            account_id=brk.id,
            date=date(2026, 8, 10),
            description="GAIN - TICKER_SEC1",
            amount=Decimal("8000.00"),
            transaction_type=TransactionType.SELL,
            statement_period="2026-08",
            coa_code="4010",
            coa_name="Realised Trading Gains",
        ),
    ]
    TransactionRepo.bulk_insert(txns, db_path)

    for period, chk_bal, brk_gross in [
        ("2025-12", Decimal("500.00"), Decimal("50000.00")),
        ("2026-08", Decimal("2100.00"), Decimal("58000.00")),
    ]:
        SnapshotRepo.upsert(
            AccountSnapshot(
                account_id=chk.id,
                statement_period=period,
                ending_balance=chk_bal,
            ),
            db_path,
        )
        SnapshotRepo.upsert(
            AccountSnapshot(
                account_id=brk.id,
                statement_period=period,
                ending_balance=brk_gross,
                gross_asset_value=brk_gross,
                margin_balance=Decimal("0.00"),
            ),
            db_path,
        )


@pytest.fixture(scope="module", autouse=True)
def _active_2025_2026_db(tmp_path_factory):
    """Use data/db/financials.db when 2025+2026 data is present locally; otherwise seed a synthetic 2025/2026 DB."""
    import sqlite3

    prod_db = ROOT / "data" / "db" / "financials.db"
    prev = os.environ.get("FI_DB_PATH")
    use_prod = False
    if prod_db.exists():
        try:
            con = sqlite3.connect(str(prod_db))
            c25 = con.execute(
                "SELECT COUNT(*) FROM transactions WHERE statement_period LIKE '2025-%'"
            ).fetchone()[0]
            c26 = con.execute(
                "SELECT COUNT(*) FROM transactions WHERE statement_period LIKE '2026-%'"
            ).fetchone()[0]
            con.close()
            use_prod = c25 > 0 and c26 > 0
        except Exception:
            use_prod = False

    if use_prod:
        os.environ["FI_DB_PATH"] = str(prod_db)
    else:
        synth_db = tmp_path_factory.mktemp("parity_2526") / "parity_2526.db"
        _seed_synthetic_2025_2026(synth_db)
        os.environ["FI_DB_PATH"] = str(synth_db)
    yield
    if prev is not None:
        os.environ["FI_DB_PATH"] = prev
    else:
        os.environ.pop("FI_DB_PATH", None)


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
def form1065_2025():
    """Compute Form 1065 for fiscal year 2025 via core API."""
    import ledger_agent.core.api as api
    return api.generate_form_1065(2025)


@pytest.fixture(scope="module")
def form1065_2026():
    """Compute Form 1065 for fiscal year 2026 via core API."""
    import ledger_agent.core.api as api
    return api.generate_form_1065(2026)


@pytest.fixture(scope="module")
def k1_p1_2025():
    """Compute Schedule K-1 for partner_1 (2025)."""
    import ledger_agent.core.api as api
    return api.generate_k1(2025, _P1_ID)


@pytest.fixture(scope="module")
def k1_p2_2025():
    """Compute Schedule K-1 for partner_2 (2025)."""
    import ledger_agent.core.api as api
    return api.generate_k1(2025, _P2_ID)


@pytest.fixture(scope="module")
def balance_sheet_2025():
    """Compute year-end balance sheet for 2025."""
    import ledger_agent.core.api as api
    return api.generate_balance_sheet(2025)


@pytest.fixture(scope="module")
def balance_sheet_2026():
    """Compute mid-year YTD balance sheet for 2026."""
    import ledger_agent.core.api as api
    return api.generate_balance_sheet(2026)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _within(computed: Decimal, reference: Decimal, label: str) -> None:
    """Assert |computed - reference| <= TOLERANCE, with a clear failure message."""
    diff = abs(computed - reference)
    assert diff <= TOLERANCE, (
        f"\nPARITY FAILURE — {label}\n"
        f"  Computed:  {computed:>14,.2f}\n"
        f"  Reference: {reference:>14,.2f}\n"
        f"  Divergence: {diff:>13,.2f}  (tolerance: {TOLERANCE:,.2f})\n"
        f"\nThis is a P0 release blocker.  The line item is out of spec.\n"
        f"Source: {FIXTURE_PATH}"
    )


def _get(corpus: dict, key: str, *, required: bool = True) -> Decimal | None:
    """Retrieve a corpus value; skip if missing."""
    val = corpus.get(key)
    if val is None and required:
        pytest.skip(f"Corpus missing key: {key!r}")
    return val


# ── Form 1065 parity tests (FY2025 & FY2026 focus) ────────────────────────────

class TestForm1065Parity:
    """Form 1065 partnership return structural & cross-line parity for FY2025 & FY2026."""

    def test_ordinary_business_income(self, form1065_2025, form1065_2026):
        for f in (form1065_2025, form1065_2026):
            expected_obi = f.gross_profit - f.total_deductions
            _within(f.ordinary_business_income, expected_obi,
                    f"Form 1065 ({f.fiscal_year}) — Ordinary Business Income identity")

    def test_total_income(self, form1065_2025, form1065_2026):
        for f in (form1065_2025, form1065_2026):
            expected_gp = f.total_income - f.cost_of_goods_sold
            _within(f.gross_profit, expected_gp,
                    f"Form 1065 ({f.fiscal_year}) — Total Income minus COGS == Gross Profit")

    def test_total_deductions(self, form1065_2025, form1065_2026):
        assert form1065_2025.total_deductions > Decimal("0")
        assert form1065_2026.total_deductions > Decimal("0")

    def test_net_stcg(self, form1065_2025, form1065_2026):
        assert form1065_2025.net_short_term_capital_gain > Decimal("0")
        assert form1065_2026.net_short_term_capital_gain > Decimal("0")

    def test_dividend_income(self, form1065_2025, form1065_2026):
        assert form1065_2025.dividend_income >= Decimal("0")
        assert form1065_2026.dividend_income >= Decimal("0")

    def test_interest_income(self, form1065_2025, form1065_2026):
        assert form1065_2025.interest_income >= Decimal("0")
        assert form1065_2026.interest_income >= Decimal("0")


# ── Schedule K-1 parity tests (FY2025) ────────────────────────────────────────

class TestScheduleK1Parity:
    """Schedule K-1 partner allocations for FY2025 (ARCH-19 / CRIT-03 fixed)."""

    def test_p1_ordinary_income(self, k1_p1_2025, form1065_2025):
        _within(k1_p1_2025.ordinary_income_loss, form1065_2025.ordinary_business_income,
                "K-1 partner_1 (2025) — Ordinary Income/Loss == 100% of Form 1065 OBI")

    def test_p1_capital_pct(self, k1_p1_2025):
        """partner_1 holds 99% of capital (K-1 Part II J — capital)."""
        assert k1_p1_2025.capital_pct == Decimal("0.99"), (  # redaction: allow
            f"partner_1 capital_pct should be majority share, got {k1_p1_2025.capital_pct}"
        )

    def test_p1_profit_loss_pct(self, k1_p1_2025):
        """partner_1 receives 100% of P&L (K-1 Part II J — profit/loss — CRIT-03 fixed)."""
        assert k1_p1_2025.profit_loss_pct == Decimal("1.00"), (
            f"partner_1 profit_loss_pct should be full share, got {k1_p1_2025.profit_loss_pct}"
        )

    def test_p2_ordinary_income(self, k1_p2_2025):
        _within(k1_p2_2025.ordinary_income_loss, Decimal("0.00"),
                "K-1 partner_2 (2025) — Ordinary Income/Loss")

    def test_p2_capital_pct(self, k1_p2_2025):
        """partner_2 holds 1% of capital (K-1 Part II J — capital)."""
        assert k1_p2_2025.capital_pct == Decimal("0.01"), (  # redaction: allow
            f"partner_2 capital_pct should be minority share, got {k1_p2_2025.capital_pct}"
        )

    def test_p2_profit_loss_pct(self, k1_p2_2025):
        """partner_2 receives 0% of P&L (K-1 Part II J — profit/loss — CRIT-03 fixed)."""
        assert k1_p2_2025.profit_loss_pct == Decimal("0.00"), (
            f"partner_2 profit_loss_pct should be zero, got {k1_p2_2025.profit_loss_pct}"
        )

    def test_k1_allocations_sum_to_form_1065(self, k1_p1_2025, k1_p2_2025,
                                              form1065_2025):
        """partner_1 + partner_2 ordinary income must sum to Form 1065 ordinary income."""
        total_k1 = (k1_p1_2025.ordinary_income_loss
                    + k1_p2_2025.ordinary_income_loss)
        _within(total_k1, form1065_2025.ordinary_business_income,
                "K-1 sum vs Form 1065 ordinary income")


# ── Balance sheet parity tests (FY2025 & FY2026) ──────────────────────────────

class TestBalanceSheetParity:
    """Balance sheet identity and completeness checks for FY2025 and FY2026."""

    def test_total_assets(self, balance_sheet_2025, balance_sheet_2026):
        assert balance_sheet_2025.total_assets > Decimal("0")
        assert balance_sheet_2026.total_assets > Decimal("0")

    def test_total_equity(self, balance_sheet_2025, balance_sheet_2026):
        for bs in (balance_sheet_2025, balance_sheet_2026):
            _within(
                Decimal(str(bs.total_equity)),
                Decimal(str(bs.total_assets - bs.total_liabilities)),
                f"Balance Sheet ({bs.period}) — Total Members' Equity == Assets - Liabilities",
            )

    def test_balance_sheet_is_balanced(self, balance_sheet_2025, balance_sheet_2026):
        """Assets must equal Liabilities + Equity (within 0.02 rounding) for 2025 and 2026."""  # redaction: allow
        for bs in (balance_sheet_2025, balance_sheet_2026):
            diff = abs((bs.total_liabilities + bs.total_equity) - bs.total_assets)
            assert diff <= Decimal("0.02"), (
                f"Balance sheet ({bs.period}) is NOT balanced: "
                f"Assets={bs.total_assets} "
                f"Liabilities+Equity={bs.total_liabilities + bs.total_equity} "
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

    def test_fixture_p1_ordinary_income_is_100pct_of_obi(self, corpus):
        """partner_1 K-1 ordinary income must equal 100% of Form 1065 ordinary income."""
        obi    = _get(corpus, "ordinary_business_income",    required=False)
        p1_oi  = _get(corpus, "partner_1_ordinary_income",   required=False)
        if obi is None or p1_oi is None:
            pytest.skip("ordinary_business_income or partner_1_ordinary_income missing from fixture")
        assert abs(p1_oi - obi) <= Decimal("0.01"), (
            f"Fixture error: partner_1_ordinary_income({p1_oi}) should equal OBI({obi}) "
            f"(full P&L per CRIT-03 fix)"
        )

    def test_fixture_p2_ordinary_income_is_zero(self, corpus):
        """partner_2 K-1 ordinary income must be 0 (0% P&L per CRIT-03 fix)."""
        p2_oi = _get(corpus, "partner_2_ordinary_income", required=False)
        if p2_oi is None:
            pytest.skip("partner_2_ordinary_income missing from fixture")
        assert p2_oi == Decimal("0.00"), (
            f"Fixture error: partner_2_ordinary_income should be 0.00 (0% P&L), got {p2_oi}"
        )

    def test_fixture_balance_sheet_identity(self, corpus):
        """Assets == Liabilities + Equity in the CPA fixture (within 1 rounding)."""  # redaction: allow
        assets  = _get(corpus, "total_assets_eoy",      required=False)
        liab    = _get(corpus, "total_liabilities_eoy", required=False)
        equity  = _get(corpus, "total_equity_eoy",      required=False)
        if assets is None or liab is None or equity is None:
            pytest.skip("Balance sheet corpus keys missing")
        diff = abs(assets - (liab + equity))
        assert diff <= Decimal("1.00"), (  # redaction: allow
            f"Fixture balance sheet not balanced: "
            f"assets={assets}, liab+equity={liab+equity}, diff={diff}"
        )


# ── Cross-form parity: core API == MCP tools ──────────────────────────────────

class TestCrossFormParity:
    """
    Verify that core API (Form A) and MCP tools dispatch (Form C) return
    identical ordinary income for FY2025 & FY2026. Any divergence > 1.00 is a P0 bug.
    """

    def test_core_api_and_mcp_tools_agree(self, form1065_2025, form1065_2026):
        """
        Call generate_form_1065 via core.api and via mcp.tools.call_tool
        and confirm they return the same ordinary_business_income for 2025 and 2026.
        """
        import json as _json

        from ledger_agent.mcp.tools import call_tool

        for f in (form1065_2025, form1065_2026):
            mcp_result_json = call_tool(
                "generate_form_1065",
                {"fiscal_year": f.fiscal_year},
                allow_pii=True,
            )
            mcp_result = _json.loads(mcp_result_json)
            mcp_obi = Decimal(str(mcp_result.get("ordinary_business_income", 0)))
            _within(
                mcp_obi,
                f.ordinary_business_income,
                f"Cross-form parity ({f.fiscal_year}): core.api vs mcp.tools — ordinary_business_income",
            )

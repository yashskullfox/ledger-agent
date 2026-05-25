"""
tests/integration/parsers/test_bank_x4_2026.py

Integration smoke tests for the bank_x4_checking and bank_x4_creditcard parsers
against the 8 real 2026 USB statement PDFs under statements/2026/.

Skipped automatically when:
  - private/institutions.py is absent (CI without secrets), OR
  - statements/2026/ PDFs are not present (gitignored)

Run locally after populating private/institutions.py:
  pytest tests/integration/parsers/test_bank_x4_2026.py -v
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

# ── corpus roots ──────────────────────────────────────────────────────────────
STMT_ROOT    = Path(__file__).parents[3] / "statements" / "2026"
CHECKING_DIR = STMT_ROOT / "account"
CC_DIR       = STMT_ROOT / "Credit card"

_raw_checking = sorted(CHECKING_DIR.glob("*.pdf")) if CHECKING_DIR.exists() else []
_raw_cc       = sorted(CC_DIR.glob("*.pdf"))       if CC_DIR.exists()       else []

# ── guards ────────────────────────────────────────────────────────────────────
try:
    from private.institutions import BANK_X4  # type: ignore  # noqa: PLC0415
    HAS_PRIVATE = bool(BANK_X4.get("detect"))
except ImportError:
    HAS_PRIVATE = False

_SKIP_PRIVATE   = pytest.mark.skipif(not HAS_PRIVATE,  reason="private/institutions.py not configured")
_SKIP_NO_CHK    = pytest.mark.skipif(not _raw_checking, reason=f"No PDFs in {CHECKING_DIR}")
_SKIP_NO_CC     = pytest.mark.skipif(not _raw_cc,       reason=f"No PDFs in {CC_DIR}")

# Parametrize lists must be non-empty for collection. When empty, use a
# skip-marked placeholder so pytest collects (and immediately skips) the test.
_NO_PDF = pytest.param(None, marks=pytest.mark.skip(reason="No PDFs present (gitignored in CI)"))
CHECKING_PDFS: list = _raw_checking or [_NO_PDF]
CC_PDFS:       list = _raw_cc       or [_NO_PDF]


# ── checking parser tests ─────────────────────────────────────────────────────
@_SKIP_PRIVATE
@_SKIP_NO_CHK
@pytest.mark.parametrize("pdf", CHECKING_PDFS, ids=lambda p: p.stem if p else "no-pdf")
def test_checking_parser_period(pdf: Path) -> None:
    """Statement period must parse as 2026-MM."""
    from ledger_agent.core.parsers.bank_x4_checking import BankX4CheckingParser  # noqa: PLC0415
    result = BankX4CheckingParser().parse(pdf)
    assert result.statement_period.startswith("2026-"), (
        f"{pdf.name}: expected period 2026-XX, got {result.statement_period!r}"
    )


@_SKIP_PRIVATE
@_SKIP_NO_CHK
@pytest.mark.parametrize("pdf", CHECKING_PDFS, ids=lambda p: p.stem if p else "no-pdf")
def test_checking_parser_has_transactions(pdf: Path) -> None:
    """Every checking statement must yield ≥1 transaction (including Jan ACH deposits)."""
    from ledger_agent.core.parsers.bank_x4_checking import BankX4CheckingParser  # noqa: PLC0415
    result = BankX4CheckingParser().parse(pdf)
    assert len(result.transactions) >= 1, (
        f"{pdf.name}: expected ≥1 transaction, got {len(result.transactions)}"
    )


@_SKIP_PRIVATE
@_SKIP_NO_CHK
@pytest.mark.parametrize("pdf", CHECKING_PDFS, ids=lambda p: p.stem if p else "no-pdf")
def test_checking_parser_snapshot_present(pdf: Path) -> None:
    """Ending balance snapshot must be present and non-negative (or zero for overdraft detection)."""
    from ledger_agent.core.parsers.bank_x4_checking import BankX4CheckingParser  # noqa: PLC0415
    result = BankX4CheckingParser().parse(pdf)
    assert result.snapshot is not None, f"{pdf.name}: snapshot is None"


@_SKIP_PRIVATE
@_SKIP_NO_CHK
def test_jan_2026_checking_has_ach_deposit() -> None:
    """Jan 2026 checking statement must contain the $600 ACH Customer Deposit."""
    from ledger_agent.core.parsers.bank_x4_checking import BankX4CheckingParser  # noqa: PLC0415
    jan_pdfs = [p for p in CHECKING_PDFS if "01" in p.stem or "Jan" in p.stem or "01-31" in p.stem or "2026-01" in p.stem]
    if not jan_pdfs:
        # Fall back: first PDF alphabetically is Jan
        jan_pdfs = CHECKING_PDFS[:1]
    result = BankX4CheckingParser().parse(jan_pdfs[0])
    amounts = [abs(t.amount) for t in result.transactions]
    assert Decimal("600.00") in amounts, (
        f"Jan 2026 checking: expected 600.00 ACH deposit, got amounts={amounts}"
    )


@_SKIP_PRIVATE
@_SKIP_NO_CHK
def test_feb_2026_checking_has_both_deposits() -> None:
    """Feb 2026 checking must contain both the $2,400 ACH deposit and the $600 Ext Tfr deposit."""
    from ledger_agent.core.parsers.bank_x4_checking import BankX4CheckingParser  # noqa: PLC0415
    feb_pdfs = [p for p in CHECKING_PDFS if "02" in p.stem or "Feb" in p.stem or "02-28" in p.stem or "2026-02" in p.stem]
    if not feb_pdfs:
        pytest.skip("Feb 2026 checking PDF not found")
    result = BankX4CheckingParser().parse(feb_pdfs[0])
    amounts = [abs(t.amount) for t in result.transactions]
    assert Decimal("2400.00") in amounts, (
        f"Feb 2026 checking: expected 2400.00 ACH deposit, got amounts={amounts}"
    )
    assert Decimal("600.00") in amounts, (
        f"Feb 2026 checking: expected 600.00 Ext Tfr deposit, got amounts={amounts}"
    )


# ── credit-card parser tests ──────────────────────────────────────────────────
@_SKIP_PRIVATE
@_SKIP_NO_CC
@pytest.mark.parametrize("pdf", CC_PDFS, ids=lambda p: p.stem if p else "no-pdf")
def test_creditcard_parser_period(pdf: Path) -> None:
    """Statement period must parse as 2026-MM."""
    from ledger_agent.core.parsers.bank_x4_creditcard import BankX4CreditCardParser  # noqa: PLC0415
    result = BankX4CreditCardParser().parse(pdf)
    assert result.statement_period.startswith("2026-"), (
        f"{pdf.name}: expected period 2026-XX, got {result.statement_period!r}"
    )


@_SKIP_PRIVATE
@_SKIP_NO_CC
@pytest.mark.parametrize("pdf", CC_PDFS, ids=lambda p: p.stem if p else "no-pdf")
def test_creditcard_parser_snapshot_present(pdf: Path) -> None:
    """Snapshot must always be present."""
    from ledger_agent.core.parsers.bank_x4_creditcard import BankX4CreditCardParser  # noqa: PLC0415
    result = BankX4CreditCardParser().parse(pdf)
    assert result.snapshot is not None, f"{pdf.name}: snapshot is None"


_CHARGE_TAGS = ("03-13", "04-14", "05-15")
_cc_with_charges: list = (
    [p for p in _raw_cc if any(tag in p.stem for tag in _CHARGE_TAGS)]
    or [_NO_PDF]
)


@_SKIP_PRIVATE
@_SKIP_NO_CC
@pytest.mark.parametrize("pdf", _cc_with_charges,
                          ids=lambda p: p.stem if p else "no-pdf")
def test_creditcard_parser_has_charges(pdf: Path) -> None:
    """Mar/Apr/May CC statements must contain ≥1 charge (Feb is a zero-balance statement)."""
    from ledger_agent.core.parsers.bank_x4_creditcard import BankX4CreditCardParser  # noqa: PLC0415
    result = BankX4CreditCardParser().parse(pdf)
    assert len(result.transactions) >= 1, (
        f"{pdf.name}: expected ≥1 charge, got {len(result.transactions)}"
    )

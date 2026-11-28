"""
tests/unit/test_vendor_rules.py  –  C-01 word-boundary and sign-aware vendor rules

Synthetic descriptions only.  Covers the local backend's keyword rules
(``local_backend._RULES``) and its fuzzy vendor fallback:

* false positives: a vendor keyword found INSIDE another word, or a generic
  word that names an unrelated merchant, must not fire the rule;
* positives: the intended vendor descriptors (including reference digits and
  symbols glued on, as bank exports do) still match;
* sign: revenue rules fire on inflows only; an inflow on an expense rule is a
  refund that keeps the expense code (contra-expense netting, the convention
  generate_form_1065 uses: ``deductions -= amt``).
"""
from __future__ import annotations

import pytest

from ledger_agent.core.intelligence.ai_backend import local_backend
from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend

OUT = -42.0   # debit / outflow
IN = 42.0     # credit / inflow


def _code(description: str, amount: float = OUT) -> str:
    return LocalBackend().classify_transaction(description, amount)["coa_code"]


# ── false positives: (description, code the OLD substring rule produced) ──────
FALSE_POSITIVES = [
    ("OWNER DRAWS MARCH", "5010"),          # AWS ⊂ DRAWS
    ("LAWS OFFICE SUPPLY", "5010"),         # AWS ⊂ LAWS
    ("GROUPS DINNER", "5061"),              # UPS ⊂ GROUPS
    ("UPSTATE PRINTING CO", "5061"),        # UPS ⊂ UPSTATE
    ("DELTA DENTAL PREMIUM", "5100"),       # bare DELTA is not an airline
    ("SOUTHWEST GAS UTILITY", "5100"),      # bare SOUTHWEST is not an airline
    ("TEAMSTERS LOCAL DUES", "5010"),       # TEAMS ⊂ TEAMSTERS
    ("CITY SPORTS TEAMS LLC", "5010"),      # bare TEAMS is not Microsoft Teams
    ("TWITTERING BIRDS CAFE", "5031"),      # TWITTER ⊂ TWITTERING
    ("TWITTER BLUE SUBSCRIPTION", "5031"),  # bare TWITTER is not advertising
    ("THEIRS BOUTIQUE", "3040"),            # IRS ⊂ THEIRS
    ("CHAIRS AND DESKS", "3040"),           # IRS ⊂ CHAIRS
    ("ESTATE TAX ATTORNEY REVIEW", "5055"),  # STATE TAX ⊂ ESTATE TAX
    ("TAXIDERMY STUDIO", "5100"),           # TAXI ⊂ TAXIDERMY
    ("TUBERS FARM MARKET", "5100"),         # UBER ⊂ TUBERS
    ("STAPLESTON HARDWARE", "5061"),        # STAPLES ⊂ STAPLESTON
    ("ZOOMIES PET SUPPLY", "5010"),         # ZOOM ⊂ ZOOMIES
    ("SLACKLINE GEAR", "5010"),             # SLACK ⊂ SLACKLINE
    ("AUGUSTO CATERING", "5021"),           # GUSTO ⊂ AUGUSTO
    ("PINEAPPLE INC PRODUCE", "5010"),      # APPLE INC ⊂ PINEAPPLE INC
    ("XBOX.COM GAME PASS", "5010"),         # BOX.COM ⊂ XBOX.COM
    ("HILTONIA FLORIST", "5100"),           # HILTON ⊂ HILTONIA
    ("ZELLE TOMMY SAMPLEPERSON", "9000"),   # ZELLE TO ⊂ ZELLE TOMMY
    ("FIRST NATIONAL BANK", "5050"),        # fuzzy IRS ⊂ FIRST (WRatio partial)
    ("KIRSCH BAKERY", "5050"),              # fuzzy IRS ⊂ KIRSCH
]


@pytest.mark.parametrize("description,old_code", FALSE_POSITIVES)
def test_keyword_inside_other_word_does_not_fire(description, old_code):
    assert _code(description) != old_code


def test_legalzoom_no_longer_swallowed_by_zoom():
    # Old: ZOOM (software, listed earlier) matched inside LEGALZOOM.
    assert _code("LEGALZOOM ANNUAL REPORT") == "5071"


# ── positives: intended descriptors still match ───────────────────────────────
POSITIVES = [
    ("AWS EMEA 1234", "5010"),
    ("AMAZON WEB SERVICES", "5010"),
    ("AMAZON.COM*AB12CD", "5061"),
    ("AMAZON MKTPL*ZZ99", "5061"),
    ("UPS*1Z999 SHIP", "5061"),
    ("THE UPS STORE 0042", "5061"),
    ("USPS PO 0123 KIOSK", "5061"),
    ("DEBITCARDPURCHASE USPSPO 0000000 MONEY ORDER", "5061"),  # redaction: allow
    ("FEDEX OFFICE 0042", "5061"),
    ("DELTA AIR LINES 0060", "5100"),
    ("DELTA AIRLINES", "5100"),
    ("SOUTHWEST AIRLINES", "5100"),
    ("UNITED AIRLINES", "5100"),
    ("AMERICAN AIRLINES", "5100"),
    ("UBER *TRIP HELP.UBER.COM", "5100"),
    ("CITY TAXI 12", "5100"),
    ("MS TEAMS ESSENTIALS", "5010"),
    ("MICROSOFT TEAMS", "5010"),
    ("ZOOM.US 888-000-0000", "5010"),
    ("SLACK T0000 DUBLIN", "5010"),
    ("TWITTER ADS", "5031"),
    ("TWITTER ONLINE ADS", "5031"),
    ("GOOGLE ADS 0000", "5031"),
    ("IRS USATAXPYMT", "3040"),
    ("IRS TREAS 310", "3040"),
    ("STATE TAX PAYMENT", "5055"),
    ("DEPT OF REVENUE", "5055"),
    ("GUSTO PAYROLL", "5021"),
    ("PAYROLL INTUIT00000000", "5021"),
    ("INTUIT00000000", "5010"),
    ("QUICKBOOKS ONLINE", "5010"),
    ("INTUIT TRANSACTION FEE", "5020"),
    ("GOOGLE *YOUTUBE", "5010"),
    ("GOOGLE*FI", "5010"),
    ("GOOGLE STORAGE", "5010"),
    ("GOOGLE PAYMENT", "5010"),
    ("APPLE INC.", "5010"),
    ("BOX.COM", "5010"),
    ("STAPLES 0042", "5061"),
    ("SERVICE CHARGE", "5020"),
    ("SERVICE CHARGES", "5020"),
    ("MONTHLY FEE", "5020"),
    ("BANK FEES", "5020"),
    ("MARGIN INTEREST", "5030"),
    ("MARRIOTT HOTEL", "5100"),
    ("ATTORNEY RETAINER", "5071"),
    # C-13: rail wording + an own-account marker (card payoff) is still internal
    ("TRANSFER TO CREDIT CARD", "9000"),
    ("ONLINE TRANSFER FROM CREDIT CARD", "9000"),
    ("PAYMENT TO CREDIT CARD", "9000"),
    ("ZELLE TO CREDIT CARD", "9000"),
    ("WIRE TRANSFER TO CREDIT CARD", "9000"),
]


@pytest.mark.parametrize("description,code", POSITIVES)
def test_intended_vendor_still_matches(description, code):
    assert _code(description) == code


def test_fuzzy_typo_still_matches():
    result = LocalBackend().classify_transaction("QUIKBOOKS ONLINE", OUT)
    assert result["coa_code"] == "5010"
    assert result["reason"].startswith("Fuzzy match")


def test_every_generic_rule_has_a_left_letter_boundary():
    generic = local_backend._RULES[len(local_backend._INTRA_XFER_KEYWORDS):]
    for pattern, *_rest in generic:
        assert pattern.startswith(local_backend._NOT_LETTER_BEFORE), pattern


def test_privacy_vocabulary_still_reads_vendor_words():
    # core/privacy.py exempts the local rules' upper-case words from name detection.
    from ledger_agent.core.privacy import _local_vendor_vocabulary
    vocab = _local_vendor_vocabulary()
    # Singular forms must survive the optional-plural rewrites ("FEE(?:S)?").
    for word in ("aws", "ups", "usps", "delta", "teams", "twitter", "amazon", "quickbooks",
                 "charge", "fee", "dividend", "gain", "sale"):
        assert word in vocab


# ── sign ──────────────────────────────────────────────────────────────────────
def test_expense_vendor_debit_is_expense_not_refund():
    result = LocalBackend().classify_transaction("AMAZON.COM*AB12", OUT)
    assert result["coa_code"] == "5061"
    assert result["confidence"] == 0.90
    assert "refund" not in result


def test_expense_vendor_credit_is_refund_on_same_code():
    # Contra-expense: a positive amount on the expense code reduces the expense
    # (generate_form_1065: deductions -= amt / cogs -= amt).
    result = LocalBackend().classify_transaction("AMAZON.COM REFUND", IN)
    assert result["coa_code"] == "5061"
    assert result["refund"] is True
    assert result["confidence"] == local_backend._REFUND_CONFIDENCE
    assert "refund" in result["reason"]


def test_fuzzy_expense_vendor_credit_is_refund():
    result = LocalBackend().classify_transaction("QUIKBOOKS CREDIT", IN)
    assert result["coa_code"] == "5010"
    assert result["refund"] is True


def test_refund_confidence_still_auto_applies():
    from ledger_agent.core.config import LOCAL_CONFIDENCE_THRESHOLD
    assert local_backend._REFUND_CONFIDENCE >= LOCAL_CONFIDENCE_THRESHOLD


@pytest.mark.parametrize("description,revenue_code", [
    ("DIVIDEND REINVEST", "4021"),
    ("DIV REINV TICKER_SEC1", "4021"),
    ("INTEREST EARNED", "4031"),
    ("INTEREST CREDIT", "4031"),
])
def test_revenue_rule_does_not_fire_on_outflow(description, revenue_code):
    assert _code(description, OUT) != revenue_code
    assert _code(description, IN) == revenue_code


def test_realized_loss_outflow_is_trading_loss():
    assert _code("REALIZED LOSS TICKER_SEC1", OUT) == "5070"
    assert _code("REALIZED GAIN TICKER_SEC1", IN) == "4010"


def test_zero_amount_applies_no_sign_filter():
    assert _code("DIVIDEND", 0.0) == "4021"
    result = LocalBackend().classify_transaction("QUICKBOOKS ONLINE", 0.0)
    assert result["coa_code"] == "5010" and "refund" not in result


@pytest.mark.parametrize("amount", [OUT, IN])
def test_transfer_and_equity_rules_ignore_sign(amount):
    assert _code("TRANSFER TO CREDIT CARD", amount) == "9000"
    assert _code("IRS USATAXPYMT", amount) == "3040"

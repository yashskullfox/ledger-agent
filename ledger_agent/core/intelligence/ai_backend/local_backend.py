"""
intelligence/ai_backend/local_backend.py  –  Local rules-based AI backend
───────────────────────────────────────────────────────────────────────────
No API key required.  Uses:
  1. Keyword rules (ordered from most- to least-specific)
  2. rapidfuzz WRatio fuzzy matching against known vendor names
  3. Amount-sign heuristics for fallback classification

Rules match whole words only, and are sign-aware: revenue rules fire on
inflows only, and an inflow that matches an expense rule is a refund (same
expense code, contra-expense netting) — see ``_rule_result`` (C-01).
  4. Usage tracking for self-learning (records how often each rule fires)

Self-learning:
  When a user confirms a classification interactively, the backend:
  - Increments the rule's confirmed_count
  - Surfaces a prompt suggesting the user commit the updated memory file
    to version control if confirmed_count crosses thresholds (3, 10, 25).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ledger_agent.core.intelligence.ai_backend.base import AIBackend

_log = logging.getLogger(__name__)

# Intra-bank transfer keywords are institution-specific and live OUTSIDE source.
# Real keywords are loaded from private/institutions.py (gitignored). If the
# private module is absent, only the generic transfer rules below apply.
try:
    from private.institutions import BANK_X as _BANK_X_CFG  # type: ignore[import-not-found]
except ImportError:
    _BANK_X_CFG = {}
_INTRA_XFER_KEYWORDS: list[str] = _BANK_X_CFG.get("intra_xfer_keywords", [])

# Each tuple: (regex_pattern, coa_code, coa_name, is_transfer)
# Listed most-specific first.
#
# C-01 word boundaries: every vendor / keyword is wrapped in letter boundaries
# (``_w``) so "AWS" no longer matches "DRAWS", "UPS" no longer matches "GROUPS",
# "IRS" no longer matches "THEIRS" and "TAXI" no longer matches "TAXIDERMY".
# A *letter* boundary is used rather than ``\b`` because bank exports glue
# reference digits and symbols onto merchant names ("INTUIT35986178",
# "AMAZON.COM*AB12", "GOOGLE*FI"): ``\b`` treats digits as word characters and
# would reject "INTUIT35986178", while a digit or symbol next to the keyword is
# no evidence of a different word.  Where a keyword is deliberately a prefix of
# longer descriptors ("GOOGLE STO" for STORE/STORAGE, "INTUIT TRAN" for
# TRANSACTION) only the left boundary is applied.
#
# Sign (C-01): rules whose code is revenue (4xxx) only fire on inflows; expense
# rules (5xxx) on an inflow are reported as a refund (see classify_transaction).
_NOT_LETTER_BEFORE = r"(?<![^\W\d_])"
_NOT_LETTER_AFTER = r"(?![^\W\d_])"


def _w(alternatives: str) -> str:
    """Wrap a regex alternation in letter boundaries on both sides."""
    return f"{_NOT_LETTER_BEFORE}(?:{alternatives}){_NOT_LETTER_AFTER}"


def _lw(alternatives: str) -> str:
    """Left letter boundary only: the keyword may be a prefix of a longer word."""
    return f"{_NOT_LETTER_BEFORE}(?:{alternatives})"


# C-13: account names that mark the counterparty of a transfer as the entity's own
# (the same card-payoff cue the bank parsers use).  "SAVINGS" / "CHECKING" are not
# markers: a third party's account has those names too.
_OWN_ACCOUNT_MARKERS = r"CREDIT\s*CARD"


_RULES: list[tuple[str, str, str, bool]] = [
    # Codes and names match _DEFAULT_COA in core/database.py exactly.
    # Listed most-specific first so the first match wins.

    # Transfers (9000) — institution-specific keywords first, then rail wording that
    # names an own account.  C-13: ZELLE / WIRE / "TRANSFER TO|FROM" wording alone
    # also carries third-party payments, so it no longer makes a row internal; it
    # needs an own-account marker (card payoff).  Bank parsers tag other rail rows
    # ``transfer_candidate`` and the import-time opposite-leg pass decides those;
    # brokerage sweeps are matched by the classifier's own-account terms.
    *[(re.escape(_kw), "9000", "Inter-Account Transfer", True) for _kw in _INTRA_XFER_KEYWORDS],
    (_w(r"(?:TRANSFER\s*(?:INCOMING|INTO|IN|OUTGOING|OUT|TO|FROM)(?:\s*(?:TO|FROM))?"
        r"|ZELLE\s*(?:TO|FROM)"
        r"|WIRE\s*(?:INCOMING|IN|OUTGOING|OUT|TRANSFER)(?:\s*(?:TO|FROM))?"
        r"|(?:PAYMENT|PYMT)\s*(?:TO|FROM))"
        r"\s*(?:" + _OWN_ACCOUNT_MARKERS + r")"),
     "9000", "Inter-Account Transfer", True),

    # Members Distributions / Owner Draws (3040)
    # V7 fix: USATAXPYMT / IRS estimated-tax payments are partner draws on a
    # pass-through LLC (ENTITY_A files Form 1065) — NOT a corporate income-tax
    # expense. Booking them as 5050 overstated operating expenses and understated
    # equity distributions. Reclassify to COA 3040 so they appear on the equity
    # schedule and are excluded from the income-statement expense lines.
    (_w(r"IRS\s*USATAXPYMT"), "3040", "Members Distributions / Owner Draws", False),
    (_w(r"USATAXPYMT"), "3040", "Members Distributions / Owner Draws", False),
    (_w(r"IRS"), "3040", "Members Distributions / Owner Draws", False),

    # State & Local Taxes (5055)
    (_w(r"STATE\s*TAX(?:ES)?|DEPT\s*OF\s*(?:REVENUE|REV)"), "5055", "State & Local Taxes", False),

    # Payroll & Wages (5021) vs Payroll Tax (5040) — order matters: vendor rule first
    (_w(r"ADP\s*PAYROLL|GUSTO"), "5021", "Payroll & Wages", False),
    # Intuit Payroll (QuickBooks Payroll) = wages
    (_w(r"PAYROLL\s+INTUIT|INTUIT\s+PAYROLL"), "5021", "Payroll & Wages", False),
    (_w(r"PAYROLL"), "5040", "Payroll Tax Expense", False),

    # Software & Subscriptions (5010)
    (_w(r"QUICKBOOKS") + "|" + _w(r"INTUIT") + r"(?!\s*TRAN)",
     "5010", "Software & Subscriptions", False),
    (_lw(r"GOOGLE\s*(?:WORKSPACE|LLC|FI|STO|PAYME)") + r"(?!\s*ADS)",
     "5010", "Software & Subscriptions", False),
    # GOOGLE *FI, GOOGLE *YOUTUBE, etc.
    (_lw(r"GOOGLE\s*\*") + r"(?!\s*ADS)", "5010", "Software & Subscriptions", False),
    (_w(r"YOUTUBE|DUCKDUCKGO|IONOS|WASHPOST|I3\s*BROADBAND|I3BROADBAND|APPLE\s*INC"),
     "5010", "Software & Subscriptions", False),
    (_w(r"MICROSOFT|MSFT"), "5010", "Software & Subscriptions", False),
    (_w(r"ADOBE"), "5010", "Software & Subscriptions", False),
    (_w(r"DROPBOX|BOX\.COM"), "5010", "Software & Subscriptions", False),
    # Bare "TEAMS" is too generic (any sports / staffing "TEAMS"); "MICROSOFT TEAMS"
    # is already caught by the MICROSOFT rule above.
    (_w(r"ZOOM|WEBEX|MS\s*TEAMS"), "5010", "Software & Subscriptions", False),
    (_w(r"SLACK"), "5010", "Software & Subscriptions", False),
    (_w(r"GITHUB|GITLAB"), "5010", "Software & Subscriptions", False),
    (_w(r"AWS|AMAZON\s*WEB\s*SERVICES|AZURE|GCP|GOOGLE\s*CLOUD"),
     "5010", "Software & Subscriptions", False),

    # Legal & Professional Fees (5071)
    (_w(r"INCFILE|BIZEE|REGISTERED\s*AGENT|NORTHWEST\s*REGISTERED"),
     "5071", "Legal & Professional Fees", False),
    (_w(r"LEGALZOOM|ROCKET\s*LAWYER|HACKING\s*LAW|LAW|ATTORNEY"),
     "5071", "Legal & Professional Fees", False),

    # Bank & Transaction Fees (5020)
    (_w(r"SERVICE\s*CHARGE(?:S)?|MONTHLY\s*FEE(?:S)?|BANK\s*FEE(?:S)?"), "5020", "Bank & Transaction Fees", False),
    (_w(r"TRAN\s*FEE(?:S)?|WIRE\s*FEE(?:S)?|NSF\s*FEE(?:S)?"), "5020", "Bank & Transaction Fees", False),
    (_lw(r"INTUIT\s*TRAN"), "5020", "Bank & Transaction Fees", False),

    # Margin Interest Expense (5030)
    (_w(r"MARGIN\s*INTEREST"), "5030", "Margin Interest Expense", False),

    # Advertising & Marketing (5031)
    (_w(r"META\s*ADS|FACEBOOK\s*ADS|INSTAGRAM\s*ADS"), "5031", "Advertising & Marketing", False),
    # A bare "TWITTER" charge is not necessarily advertising (subscriptions,
    # any merchant name containing the word): require the ads qualifier.
    (_w(r"TWITTER(?:\s*ONLINE)?\s*ADS|X\.COM\s*ADS|GOOGLE\s*ADS"),
     "5031", "Advertising & Marketing", False),

    # Office & Shipping Supplies (5061)
    (_w(r"OFFICE\s*DEPOT|STAPLES|SAMS\s*CLUB|SAMSCLUB|EPSON")
     + "|" + _w(r"AMAZON") + r"(?!\s*WEB)",
     "5061", "Office & Shipping Supplies", False),
    # USPSPO = USPS post office counter
    (_w(r"FEDEX|UPS|USPS(?:PO)?"), "5061", "Office & Shipping Supplies", False),

    # Travel & Transportation (5100)
    # A bare "DELTA" / "SOUTHWEST" also names dental plans, faucets, gas
    # utilities and banks: require the airline qualifier (AIR / AIRLINES).
    (_w(r"(?:DELTA|UNITED|SOUTHWEST|AMERICAN)\s*(?:AIRLINES|AIRLINE|AIRWAYS|AIR)"),
     "5100", "Travel & Transportation", False),
    (_w(r"UBER|LYFT|TAXI"), "5100", "Travel & Transportation", False),
    (_w(r"MARRIOTT|HILTON|HYATT|AIRBNB"), "5100", "Travel & Transportation", False),

    # Revenue / income — inflows only (see classify_transaction)
    (_w(r"DIVIDEND(?:S)?") + "|" + _lw(r"DIV\s*REINV"), "4021", "Dividend Income", False),
    (_w(r"INTEREST\s*(?:EARNED|CREDIT(?:ED|S)?)"), "4031", "Interest Income", False),
    (_w(r"REALIZED\s*(?:GAIN(?:S)?|LOSS(?:ES)?)|PROCEEDS\s*FROM\s*SALE(?:S)?"),
     "4010", "Realised Trading Gains", False),
]

_COMPILED_RULES = [
    (re.compile(pat, re.IGNORECASE), code, name, xfer)
    for pat, code, name, xfer in _RULES
]

# Letter runs of a description, for word-level fuzzy matching.
_WORD_RE = re.compile(r"[^\W\d_]+")

# Revenue rules that have an expense counterpart for outflows: a realised
# trading LOSS is 5070 (the brokerage parsers use the same pair).  Other
# revenue rules are skipped on outflows: a reinvestment debit buys shares,
# so it falls through to review instead.
_OUTFLOW_COUNTERPART: dict[str, tuple[str, str]] = {
    "4010": ("5070", "Realised Trading Losses"),
}

# Confidence for an INFLOW that matched an expense rule.  Lower than a debit
# match (0.90) because a credit from a vendor may also be a payout, but still
# above the default escalation threshold, so it is kept as a refund.
_REFUND_CONFIDENCE = 0.75


def _rule_result(
        code: str,
        name: str,
        xfer: bool,
        amount: float,
        confidence: float,
        reason: str,
) -> dict[str, Any]:
    """Build a classification result, treating expense codes on inflows as refunds.

    C-01 sign rule: an inflow that matches an expense vendor (5xxx) is a refund
    or reversal of that expense.  It keeps the expense code because the ledger's
    convention is contra-expense netting: generate_form_1065 does
    ``deductions -= amt`` (and ``cogs -= amt``), so a positive amount on the same
    code REDUCES the expense.  Coding it as revenue (4020) would overstate gross
    receipts, and Uncategorized (5999) would lose the vendor.
    """
    result: dict[str, Any] = {
        "coa_code": code,
        "coa_name": name,
        "is_transfer": xfer,
        "confidence": confidence,
        "reason": reason,
    }
    if amount > 0 and code.startswith("5"):
        result["refund"] = True
        result["confidence"] = min(confidence, _REFUND_CONFIDENCE)
        result["reason"] = f"{reason}; inflow → refund (reduces '{name}')"
    return result


_usage_counts: dict[str, int] = {}
_COMMIT_THRESHOLDS = {3, 10, 25}


class LocalBackend(AIBackend):
    """Rule-based backend with rapidfuzz fuzzy matching and usage tracking."""

    @property
    def backend_name(self) -> str:
        return "local"

    def classify_transaction(
            self,
            description: str,
            amount: float,
            context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        desc_up = description.upper()

        # 1. Keyword rules (sign-aware, C-01)
        for pattern, code, name, xfer in _COMPILED_RULES:
            if not pattern.search(desc_up):
                continue
            if amount < 0 and code.startswith("4"):
                counterpart = _OUTFLOW_COUNTERPART.get(code)
                if counterpart is None:
                    continue  # an outflow is never income: try the next rule
                code, name = counterpart
            _usage_counts[code] = _usage_counts.get(code, 0) + 1
            return _rule_result(code, name, xfer, amount, 0.90, f"Matched rule pattern for '{name}'")

        # 2. rapidfuzz fuzzy match against known vendor names
        try:
            from rapidfuzz import fuzz, process

            from ledger_agent.core.config import AUTO_CLASSIFY_THRESHOLD

            _vendors = {
                "INCFILE": ("5071", "Legal & Professional Fees", False),
                "QUICKBOOKS": ("5010", "Software & Subscriptions", False),
                "GOOGLE": ("5010", "Software & Subscriptions", False),
                "IRS": ("5050", "Federal Income Tax Expense", False),
                "PAYROLL": ("5021", "Payroll & Wages", False),
                "AMAZON": ("5061", "Office & Shipping Supplies", False),
            }
            # C-01: compare whole words, not substrings. WRatio over the full
            # description scores a short vendor found INSIDE a word ("IRS" in
            # "FIRST", "KIRSCH") at 90; a per-word ratio still tolerates typos
            # ("QUIKBOOKS") but needs the word itself to resemble the vendor.
            best: tuple[str, float] | None = None
            for word in _WORD_RE.findall(desc_up):
                hit = process.extractOne(
                    word, list(_vendors.keys()),
                    scorer=fuzz.ratio,
                    score_cutoff=AUTO_CLASSIFY_THRESHOLD,
                )
                if hit and (best is None or hit[1] > best[1]):
                    best = (hit[0], hit[1])
            if best:
                vendor, score = best
                code, name, xfer = _vendors[vendor]
                return _rule_result(
                    code, name, xfer, amount, round(score / 100, 2),
                    f"Fuzzy match: '{vendor}' (score {score:.0f})",
                )
        except ImportError:
            pass

        # 3. Amount heuristic fallback
        if amount > 0:
            return {
                "coa_code": "4020",
                "coa_name": "Service Revenue",
                "is_transfer": False,
                "confidence": 0.30,
                "reason": "Positive amount → likely revenue (manual review needed)",
            }
        return {
            "coa_code": "5999",
            "coa_name": "Uncategorized Expense",
            "is_transfer": False,
            "confidence": 0.20,
            "reason": "No pattern matched – please classify manually",
        }

    def enhance_memory_rule(
            self,
            pattern: str,
            coa_code: str,
            confirmed_count: int,
    ) -> dict[str, Any]:
        # Generate simple variations: strip numbers, add wildcards
        cleaned = re.sub(r"\d+", "", pattern).strip()
        aliases = [cleaned] if cleaned != pattern else []
        if len(pattern) > 6:
            aliases.append(pattern[:6])
        return {
            "enhanced_pattern": cleaned or pattern,
            "suggested_aliases": aliases[:3],
            "confidence": min(0.5 + confirmed_count * 0.05, 0.95),
        }

    def explain_classification(
            self,
            description: str,
            coa_code: str,
            coa_name: str,
    ) -> str:
        return (
            f"Transaction '{description}' mapped to [{coa_code}] {coa_name} "
            f"via local rule-based matching."
        )

    def on_user_confirmed(
            self,
            description: str,
            coa_code: str,
            coa_name: str,
            is_transfer: bool,
    ) -> None:
        """Track confirmations and log a periodic progress note."""
        _usage_counts[coa_code] = _usage_counts.get(coa_code, 0) + 1
        count = _usage_counts[coa_code]
        if count in _COMMIT_THRESHOLDS:
            try:
                _log.info(
                    "Classification memory: %d transactions confirmed under '%s'. "
                    "Rules are saved locally in the data directory (gitignored; "
                    "never commit it -- it is derived from real transactions).",
                    count, coa_name,
                )
            except Exception:
                pass

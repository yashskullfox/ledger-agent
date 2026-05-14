"""
intelligence/ai_backend/local_backend.py  –  Local rules-based AI backend
───────────────────────────────────────────────────────────────────────────
No API key required.  Uses:
  1. Keyword rules (ordered from most- to least-specific)
  2. rapidfuzz WRatio fuzzy matching against known vendor names
  3. Amount-sign heuristics for fallback classification
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
from typing import Any, Dict, List, Optional, Tuple

from ledger_agent.core.intelligence.ai_backend.base import AIBackend

_log = logging.getLogger(__name__)

# Each tuple: (regex_pattern, coa_code, coa_name, is_transfer)
# Listed most-specific first.

_RULES: List[Tuple[str, str, str, bool]] = [
    # Codes and names match _DEFAULT_COA in core/database.py exactly.
    # Listed most-specific first so the first match wins.

    # Transfers (9000)
    (r"MONEYLINE\s*FID", "9000", "Inter-Account Transfer", True),
    (r"TRANSFER\s*(IN|OUT|TO|FROM)", "9000", "Inter-Account Transfer", True),
    (r"ZELLE\s*TO|ZELLE\s*FROM", "9000", "Inter-Account Transfer", True),
    (r"WIRE\s*(IN|OUT|TRANSFER)", "9000", "Inter-Account Transfer", True),

    # Members Distributions / Owner Draws (3040)
    # V7 fix: USATAXPYMT / IRS estimated-tax payments are partner draws on a
    # pass-through LLC (SYNCED LLC files Form 1065) — NOT a corporate income-tax
    # expense. Booking them as 5050 overstated operating expenses and understated
    # equity distributions. Reclassify to COA 3040 so they appear on the equity
    # schedule and are excluded from the income-statement expense lines.
    (r"IRS\s*USATAXPYMT", "3040", "Members Distributions / Owner Draws", False),
    (r"USATAXPYMT", "3040", "Members Distributions / Owner Draws", False),
    (r"IRS\b", "3040", "Members Distributions / Owner Draws", False),

    # State & Local Taxes (5055)
    (r"STATE\s*TAX|DEPT\s*OF\s*REV", "5055", "State & Local Taxes", False),

    # Payroll & Wages (5021) vs Payroll Tax (5040) — order matters: vendor rule first
    (r"ADP\s*PAYROLL|GUSTO", "5021", "Payroll & Wages", False),
    (r"PAYROLL\s+INTUIT|INTUIT\s+PAYROLL", "5021", "Payroll & Wages", False),  # Intuit Payroll (QuickBooks Payroll) = wages
    (r"\bPAYROLL\b", "5040", "Payroll Tax Expense", False),

    # Software & Subscriptions (5010)
    (r"QUICKBOOKS|INTUIT(?!\s*TRAN)", "5010", "Software & Subscriptions", False),
    (r"GOOGLE\s*(WORKSPACE|LLC)(?!\s*ADS)", "5010", "Software & Subscriptions", False),
    (r"GOOGLE\s*\*(?!\s*ADS)", "5010", "Software & Subscriptions", False),  # GOOGLE *FI, GOOGLE *YOUTUBE, etc.
    (r"YOUTUBE", "5010", "Software & Subscriptions", False),
    (r"MICROSOFT|MSFT", "5010", "Software & Subscriptions", False),
    (r"ADOBE", "5010", "Software & Subscriptions", False),
    (r"DROPBOX|BOX\.COM", "5010", "Software & Subscriptions", False),
    (r"ZOOM|WEBEX|TEAMS", "5010", "Software & Subscriptions", False),
    (r"SLACK", "5010", "Software & Subscriptions", False),
    (r"GITHUB|GITLAB", "5010", "Software & Subscriptions", False),
    (r"AWS|AMAZON\s*WEB\s*SERVICES|AZURE|GCP|GOOGLE\s*CLOUD", "5010", "Software & Subscriptions", False),

    # Legal & Professional Fees (5071)
    (r"INCFILE|REGISTERED\s*AGENT|NORTHWEST\s*REGISTERED", "5071", "Legal & Professional Fees", False),
    (r"LEGALZOOM|ROCKET\s*LAWYER", "5071", "Legal & Professional Fees", False),

    # Bank & Transaction Fees (5020)
    (r"SERVICE\s*CHARGE|MONTHLY\s*FEE|BANK\s*FEE", "5020", "Bank & Transaction Fees", False),
    (r"TRAN\s*FEE|WIRE\s*FEE|NSF\s*FEE", "5020", "Bank & Transaction Fees", False),
    (r"INTUIT\s*TRAN", "5020", "Bank & Transaction Fees", False),

    # Margin Interest Expense (5030)
    (r"MARGIN\s*INTEREST", "5030", "Margin Interest Expense", False),

    # Advertising & Marketing (5031)
    (r"META\s*ADS|FACEBOOK\s*ADS|INSTAGRAM\s*ADS", "5031", "Advertising & Marketing", False),
    (r"TWITTER|X\.COM\s*ADS|GOOGLE\s*ADS", "5031", "Advertising & Marketing", False),

    # Office & Shipping Supplies (5061)
    (r"OFFICE\s*DEPOT|STAPLES|AMAZON(?!\s*WEB)", "5061", "Office & Shipping Supplies", False),
    (r"FEDEX|UPS|USPS", "5061", "Office & Shipping Supplies", False),

    # Travel & Transportation (5100)
    (r"DELTA|UNITED\s*AIR|SOUTHWEST|AMERICAN\s*AIR", "5100", "Travel & Transportation", False),
    (r"UBER|LYFT|TAXI", "5100", "Travel & Transportation", False),
    (r"MARRIOTT|HILTON|HYATT|AIRBNB", "5100", "Travel & Transportation", False),

    # Revenue / income
    (r"DIVIDEND|DIV\s*REINV", "4021", "Dividend Income", False),
    (r"INTEREST\s*EARNED|INTEREST\s*CREDIT", "4031", "Interest Income", False),
    (r"REALIZED\s*(GAIN|LOSS)|PROCEEDS\s*FROM\s*SALE", "4010", "Realised Trading Gains", False),
]

_COMPILED_RULES = [
    (re.compile(pat, re.IGNORECASE), code, name, xfer)
    for pat, code, name, xfer in _RULES
]

_usage_counts: Dict[str, int] = {}
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
            context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        desc_up = description.upper()

        # 1. Keyword rules
        for pattern, code, name, xfer in _COMPILED_RULES:
            if pattern.search(desc_up):
                _usage_counts[code] = _usage_counts.get(code, 0) + 1
                return {
                    "coa_code": code,
                    "coa_name": name,
                    "is_transfer": xfer,
                    "confidence": 0.90,
                    "reason": f"Matched rule pattern for '{name}'",
                }

        # 2. rapidfuzz fuzzy match against known vendor names
        try:
            from rapidfuzz import fuzz, process
            from config import AUTO_CLASSIFY_THRESHOLD

            _VENDORS = {
                "INCFILE": ("5071", "Legal & Professional Fees", False),
                "QUICKBOOKS": ("5010", "Software & Subscriptions", False),
                "GOOGLE": ("5010", "Software & Subscriptions", False),
                "IRS": ("5050", "Federal Income Tax Expense", False),
                "PAYROLL": ("5021", "Payroll & Wages", False),
                "AMAZON": ("5061", "Office & Shipping Supplies", False),
            }
            best = process.extractOne(
                desc_up, list(_VENDORS.keys()),
                scorer=fuzz.WRatio,
                score_cutoff=AUTO_CLASSIFY_THRESHOLD,
            )
            if best:
                vendor, score, _ = best
                code, name, xfer = _VENDORS[vendor]
                return {
                    "coa_code": code,
                    "coa_name": name,
                    "is_transfer": xfer,
                    "confidence": round(score / 100, 2),
                    "reason": f"Fuzzy match: '{vendor}' (score {score:.0f})",
                }
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
    ) -> Dict[str, Any]:
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
        """Track confirmations and nudge user to commit memory updates."""
        _usage_counts[coa_code] = _usage_counts.get(coa_code, 0) + 1
        count = _usage_counts[coa_code]
        if count in _COMMIT_THRESHOLDS:
            try:
                _log.info(
                    "Classification memory tip: %d transactions confirmed under '%s'. "
                    "Consider committing: git add data/db/classification_memory.json && "
                    "git commit -m 'chore: update classification memory (%d rules)'",
                    count, coa_name, count,
                )
            except Exception:
                pass

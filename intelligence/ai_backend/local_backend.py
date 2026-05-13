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

import re
from typing import Any, Dict, List, Optional, Tuple

from intelligence.ai_backend.base import AIBackend

# ── Classification rules ──────────────────────────────────────────────────────
# Each tuple: (regex_pattern, coa_code, coa_name, is_transfer)
# Listed most-specific first.

_RULES: List[Tuple[str, str, str, bool]] = [
    # Transfers
    (r"MONEYLINE\s*FID", "9000", "Inter-Account Transfer", True),
    (r"TRANSFER\s*(IN|OUT|TO|FROM)", "9000", "Inter-Account Transfer", True),
    (r"ZELLE\s*TO|ZELLE\s*FROM", "9000", "Inter-Account Transfer", True),
    (r"WIRE\s*(IN|OUT|TRANSFER)", "9000", "Inter-Account Transfer", True),

    # Taxes
    (r"IRS\s*USATAXPYMT", "5050", "Federal Income Tax", False),
    (r"USATAXPYMT", "5050", "Federal Income Tax", False),
    (r"IRS\b", "5050", "Federal Income Tax", False),
    (r"STATE\s*TAX|DEPT\s*OF\s*REV", "5055", "State & Local Taxes", False),

    # Payroll
    (r"PAYROLL|ADP\s*PAYROLL|GUSTO", "5020", "Payroll & Wages", False),

    # Software / SaaS
    (r"QUICKBOOKS|INTUIT", "5010", "Software & SaaS", False),
    (r"GOOGLE\s*(WORKSPACE|ADS|LLC)", "5010", "Software & SaaS", False),
    (r"MICROSOFT|MSFT", "5010", "Software & SaaS", False),
    (r"ADOBE", "5010", "Software & SaaS", False),
    (r"DROPBOX|BOX\.COM", "5010", "Software & SaaS", False),
    (r"ZOOM|WEBEX|TEAMS", "5010", "Software & SaaS", False),
    (r"SLACK", "5010", "Software & SaaS", False),
    (r"GITHUB|GITLAB", "5010", "Software & SaaS", False),
    (r"AWS|AMAZON\s*WEB|AZURE|GCP|GOOGLE\s*CLOUD", "5010", "Cloud Infrastructure", False),

    # Legal / Registered Agent
    (r"INCFILE|REGISTERED\s*AGENT|NORTHWEST\s*REGISTERED", "5070", "Legal & Professional", False),
    (r"LEGALZOOM|ROCKET\s*LAWYER", "5070", "Legal & Professional", False),

    # Banking / Fees
    (r"SERVICE\s*CHARGE|MONTHLY\s*FEE|BANK\s*FEE", "5080", "Bank Fees", False),
    (r"TRAN\s*FEE|WIRE\s*FEE|NSF\s*FEE", "5080", "Bank Fees", False),
    (r"MARGIN\s*INTEREST", "5090", "Interest Expense", False),

    # Advertising
    (r"META\s*ADS|FACEBOOK\s*ADS|INSTAGRAM\s*ADS", "5030", "Advertising & Marketing", False),
    (r"TWITTER|X\.COM\s*ADS", "5030", "Advertising & Marketing", False),

    # Office / Supplies
    (r"OFFICE\s*DEPOT|STAPLES|AMAZON(?!\s*WEB)", "5060", "Office Supplies", False),
    (r"FEDEX|UPS|USPS", "5060", "Shipping & Office", False),

    # Travel
    (r"DELTA|UNITED\s*AIR|SOUTHWEST|AMERICAN\s*AIR", "5100", "Travel & Transportation", False),
    (r"UBER|LYFT|TAXI", "5100", "Travel & Transportation", False),
    (r"MARRIOTT|HILTON|HYATT|AIRBNB", "5100", "Travel & Lodging", False),

    # Revenue / income
    (r"DIVIDEND|DIV\s*REINV", "4020", "Dividend Income", False),
    (r"INTEREST\s*EARNED|INTEREST\s*CREDIT", "4030", "Interest Income", False),
    (r"REALIZED\s*(GAIN|LOSS)|PROCEEDS\s*FROM\s*SALE", "4010", "Realized Gains/Losses", False),
]

_COMPILED_RULES = [
    (re.compile(pat, re.IGNORECASE), code, name, xfer)
    for pat, code, name, xfer in _RULES
]

# ── Usage tracking (in-memory; persisted via memory.py) ──────────────────────
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
                "INCFILE": ("5070", "Legal & Professional", False),
                "QUICKBOOKS": ("5010", "Software & SaaS", False),
                "GOOGLE": ("5010", "Software & SaaS", False),
                "IRS": ("5050", "Federal Income Tax", False),
                "PAYROLL": ("5020", "Payroll & Wages", False),
                "AMAZON": ("5060", "Office Supplies", False),
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
                "coa_code": "4000",
                "coa_name": "General Revenue",
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
                from cli.prompts import print_info
                print_info(
                    f"[dim]💡 You've confirmed [bold]{count}[/bold] transactions under "
                    f"[yellow]{coa_name}[/yellow].  Consider committing your "
                    f"classification memory to version control:\n"
                    f"  git add data/db/classification_memory.json && "
                    f"git commit -m 'chore: update classification memory ({count} rules)'[/dim]"
                )
            except Exception:
                pass

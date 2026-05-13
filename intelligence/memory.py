"""
intelligence/memory.py  –  Persistent classification memory
─────────────────────────────────────────────────────────────
Stores user-confirmed (description → COA code) mappings in a JSON file.
Uses rapidfuzz for fuzzy lookup so "QUICKBOOKS 01-16" and "QUICKBOOKS 01-23"
both resolve to the same remembered rule.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import MEMORY_FILE, AUTO_CLASSIFY_THRESHOLD

try:
    from rapidfuzz import fuzz, process as rf_process
    _FUZZY_AVAILABLE = True
except ImportError:
    _FUZZY_AVAILABLE = False


class ClassificationMemory:
    """
    Thread-safe (single-process) persistent store for classification rules.

    Storage format (JSON):
    {
      "rules": [
        {
          "pattern":  "PAYPAL *QUICKBOOKS",    ← normalised description
          "coa_code": "5010",
          "coa_name": "Software & Subscriptions",
          "is_transfer": false,
          "confirmed_count": 3                 ← how many times user confirmed this
        },
        ...
      ]
    }
    """

    def __init__(self, memory_file: Path = MEMORY_FILE):
        self._file = memory_file
        self._rules: List[Dict] = []
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def lookup(self, description: str) -> Optional[Tuple[str, str, bool, int]]:
        """
        Try to find a remembered rule for `description`.

        Returns (coa_code, coa_name, is_transfer, score) if found, else None.
        `score` is 0-100; >= AUTO_CLASSIFY_THRESHOLD = auto-apply without asking.
        """
        if not self._rules:
            return None

        desc_norm = self._normalise(description)

        # 1. Exact match
        for r in self._rules:
            if self._normalise(r["pattern"]) == desc_norm:
                return r["coa_code"], r["coa_name"], r.get("is_transfer", False), 100

        # 2. Fuzzy match
        if _FUZZY_AVAILABLE:
            choices = {r["pattern"]: r for r in self._rules}
            result  = rf_process.extractOne(
                desc_norm,
                list(choices.keys()),
                scorer=fuzz.WRatio,
                score_cutoff=60,
            )
            if result:
                matched_pattern, score, _ = result
                r = choices[matched_pattern]
                return r["coa_code"], r["coa_name"], r.get("is_transfer", False), score

        return None

    def remember(self, description: str, coa_code: str, coa_name: str,
                 is_transfer: bool = False) -> None:
        """Persist a new (or update existing) rule."""
        desc_norm = self._normalise(description)
        for r in self._rules:
            if self._normalise(r["pattern"]) == desc_norm:
                r["coa_code"]      = coa_code
                r["coa_name"]      = coa_name
                r["is_transfer"]   = is_transfer
                r["confirmed_count"] = r.get("confirmed_count", 1) + 1
                self._save()
                return
        self._rules.append({
            "pattern":         description,
            "coa_code":        coa_code,
            "coa_name":        coa_name,
            "is_transfer":     is_transfer,
            "confirmed_count": 1,
        })
        self._save()

    def list_rules(self) -> List[Dict]:
        return list(self._rules)

    def remove_rule(self, pattern: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules
                       if self._normalise(r["pattern"]) != self._normalise(pattern)]
        if len(self._rules) < before:
            self._save()
            return True
        return False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self._rules = data.get("rules", [])
            except Exception:
                self._rules = []
        else:
            self._rules = []
            # Pre-seed with known patterns from the sample statements
            self._seed_defaults()

    def _seed_defaults(self) -> None:
        defaults = [
            ("INCFILE LLC",          "5010", "Software & Subscriptions",     False),
            ("PAYPAL *QUICKBOOKS",   "5010", "Software & Subscriptions",     False),
            ("GOOGLE",               "5010", "Software & Subscriptions",     False),
            ("USATAXPYMT IRS",       "5050", "Federal Income Tax Expense",   False),
            ("TAX PAYROLL",          "5040", "Payroll Tax Expense",          False),
            ("TRAN FEE INTUIT",      "5020", "Bank & Transaction Fees",      False),
            ("MONEYLINE FID BKG",    "3010", "Members Capital Contributions", True),
            ("DEPOSIT INTUIT",       "4020", "Service Revenue",              False),
            ("EFT FUNDS PAID",       "3010", "Members Capital Contributions", True),
            ("MARGIN INTEREST",      "5030", "Margin Interest Expense",      False),
        ]
        for pattern, code, name, is_xfer in defaults:
            self._rules.append({
                "pattern":         pattern,
                "coa_code":        code,
                "coa_name":        name,
                "is_transfer":     is_xfer,
                "confirmed_count": 0,   # 0 = seeded, not user-confirmed
            })
        self._save()

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps({"rules": self._rules}, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _normalise(text: str) -> str:
        import re
        # Upper-case, collapse spaces, strip date tokens like "01-08"
        text = text.upper().strip()
        text = re.sub(r"\b\d{2}-\d{2}\b", "", text)   # remove MM-DD
        text = re.sub(r"\b\d{6,}\b", "", text)          # remove long numbers
        text = re.sub(r"\s+", " ", text).strip()
        return text


# Module-level singleton
_memory: Optional[ClassificationMemory] = None


def get_memory() -> ClassificationMemory:
    global _memory
    if _memory is None:
        _memory = ClassificationMemory()
    return _memory

"""
intelligence/memory.py  –  Persistent classification memory
─────────────────────────────────────────────────────────────
Stores user-confirmed (description → COA code) mappings in a JSON file.
Uses rapidfuzz for fuzzy lookup so "QUICKBOOKS 01-16" and "QUICKBOOKS 01-23"
both resolve to the same remembered rule.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from ledger_agent.core.config import MEMORY_FILE

_log = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz
    from rapidfuzz import process as rf_process

    _FUZZY_AVAILABLE = True
except ImportError:
    _FUZZY_AVAILABLE = False


# C-14: rule directions.  ``in`` = inflows only, ``out`` = outflows only, ``any`` =
# either.  Rules written before C-14 carry no direction and load as ``any``.
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTION_ANY = "any"
_DIRECTIONS = frozenset({DIRECTION_IN, DIRECTION_OUT, DIRECTION_ANY})

# C-14: ``"match": "word"`` rules apply only on an exact match or when the pattern's
# words appear as a whole-word run in the description; they never fuzzy-match.
MATCH_WORD = "word"

# The internal-transfer code.  Memory never replays it (or any rule learned with
# ``is_transfer``) unless the caller vouches for an own-account cue.
_TRANSFER_CODE = "9000"

# C-14: seeded rules written by earlier versions that are tightened or dropped when
# a legacy file is loaded (only while still unconfirmed, ``confirmed_count == 0``).
# The upgrade happens in memory; the file is rewritten only by a later save.
_LEGACY_SEED_UPGRADES = {
    # Fuzzy-matched any "... DEPOSIT" (score about 85) in either direction.
    ("DEPOSIT INTUIT", "4020"): {"direction": DIRECTION_IN, "match": MATCH_WORD},
}
_LEGACY_SEEDS_DROPPED = frozenset({
    # Wording-only internal transfer (the parser pre-codes the real rows 9000).
    ("EFT FUNDS PAID", "9000"),
})


def direction_for_amount(amount: float | None) -> str:
    """``in`` for an inflow, ``out`` for an outflow, ``any`` when unknown or zero."""
    if amount is None:
        return DIRECTION_ANY
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return DIRECTION_ANY
    if value > 0:
        return DIRECTION_IN
    if value < 0:
        return DIRECTION_OUT
    return DIRECTION_ANY


def _rule_direction(rule: dict) -> str:
    direction = rule.get("direction", DIRECTION_ANY)
    return direction if direction in _DIRECTIONS else DIRECTION_ANY


def _is_transfer_rule(rule: dict) -> bool:
    return str(rule.get("coa_code", "")) == _TRANSFER_CODE or bool(rule.get("is_transfer"))


def _word_run_re(pattern_norm: str) -> re.Pattern[str]:
    words = pattern_norm.split()
    return re.compile(
        r"(?<![^\W\d_])" + r"\s+".join(re.escape(w) for w in words) + r"(?![^\W\d_])"
    )


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
          "confirmed_count": 3,                ← how many times user confirmed this
          "direction": "out",                  ← C-14: "in" / "out" / "any" (optional)
          "match": "word"                      ← C-14: optional, no fuzzy matching
        },
        ...
      ]
    }

    C-14 fields are additive: older code ignores them, and a rule without
    ``direction`` loads as ``any``.

    Lookup (C-14) is sign-aware and never wording-only for transfers:

    * a revenue rule (4xxx) applies only to inflows and an expense rule (5xxx) only
      to outflows, whatever direction it was stored with; other codes follow the
      stored direction;
    * an internal-transfer rule (9000, or learned with ``is_transfer``) applies only
      when the caller passes ``own_account_cue=True`` (C-03 / C-13: a row is internal
      only on a parser own-account cue or a confirmed opposite leg).
    """

    def __init__(self, memory_file: Path = MEMORY_FILE):
        self._file = memory_file
        self._rules: list[dict] = []
        self._load()

    def lookup(self, description: str, amount: float | None = None, *,
               own_account_cue: bool = False) -> tuple[str, str, bool, int] | None:
        """
        Try to find a remembered rule for `description`.

        `amount` is the signed transaction amount (positive = inflow); ``None`` or
        zero means the direction is unknown and no direction filter is applied.
        `own_account_cue` must be True for an internal-transfer rule to apply (C-14);
        the default fails closed.

        The best-matching rule is found first (exact, then whole-word rules, then
        fuzzy) and is then checked against the amount and cue.  If it does not apply,
        the result is None: a weaker match is not promoted in its place, because the
        runner-up of a fuzzy search is usually an unrelated rule.

        Returns (coa_code, coa_name, is_transfer, score) if found, else None.
        `score` is 0-100; >= AUTO_CLASSIFY_THRESHOLD = auto-apply without asking.
        """
        if not self._rules:
            return None

        desc_norm = self._normalise(description)

        def hit(rule: dict, score: float) -> tuple[str, str, bool, int] | None:
            if not self._applies(rule, amount, own_account_cue):
                return None
            return rule["coa_code"], rule["coa_name"], rule.get("is_transfer", False), score

        # 1. Exact match (the first one that applies, if several share the pattern)
        exact = [r for r in self._rules if self._normalise(r["pattern"]) == desc_norm]
        if exact:
            return next((h for h in (hit(r, 100) for r in exact) if h), None)

        # 2. Whole-word rules (C-14): the pattern's words as a run, never fuzzy
        for r in self._rules:
            if r.get("match") == MATCH_WORD:
                pattern_norm = self._normalise(r["pattern"])
                if pattern_norm and _word_run_re(pattern_norm).search(desc_norm):
                    return hit(r, 100)

        # 3. Fuzzy match
        fuzzy_rules = [r for r in self._rules if r.get("match") != MATCH_WORD]
        if _FUZZY_AVAILABLE and fuzzy_rules:
            choices = {r["pattern"]: r for r in fuzzy_rules}
            result = rf_process.extractOne(
                desc_norm,
                list(choices.keys()),
                scorer=fuzz.WRatio,
                score_cutoff=60,
            )
            if result:
                matched_pattern, score, _ = result
                return hit(choices[matched_pattern], score)

        return None

    @staticmethod
    def _applies(rule: dict, amount: float | None, own_account_cue: bool) -> bool:
        """C-14: whether `rule` may be used for a row with this amount / cue."""
        if _is_transfer_rule(rule) and not own_account_cue:
            return False
        direction = direction_for_amount(amount)
        if direction == DIRECTION_ANY:
            return True
        family = str(rule.get("coa_code", ""))[:1]
        if family == "4":  # revenue: inflows only
            return direction == DIRECTION_IN
        if family == "5":  # expense: outflows only
            return direction == DIRECTION_OUT
        stored = _rule_direction(rule)
        return stored in (DIRECTION_ANY, direction)

    def remember(self, description: str, coa_code: str, coa_name: str,
                 is_transfer: bool = False, amount: float | None = None) -> None:
        """
        Persist a new (or update existing) rule.

        `amount` (C-14) records the rule's direction: ``in`` for an inflow, ``out``
        for an outflow, ``any`` when not given.  Re-confirming a rule with the same
        code in the other direction widens it to ``any``; a new code takes the new
        direction.
        The pattern is redacted via privacy.redact() before writing to disk
        so the on-disk JSON is git-safe — no raw counterparty names (R-46).

        Fail closed (P-06): if redaction fails, the rule is NOT saved — the raw
        pattern never reaches disk.  The user's confirmation itself still succeeds
        (nothing is raised); only the remembered rule is skipped.
        """
        # Redact PII from the pattern before persisting (R-46)
        try:
            from ledger_agent.core import privacy
            safe_description, _ = privacy.redact(description, scope="memory_file")
            if not isinstance(safe_description, str):
                raise TypeError("redact() returned a non-string")
        except Exception as exc:  # noqa: BLE001 — fail closed: skip, never persist raw
            _log.warning(
                "memory rule not saved: pattern redaction failed (%s)", type(exc).__name__
            )
            return

        direction = direction_for_amount(amount)
        desc_norm = self._normalise(safe_description)
        for r in self._rules:
            if self._normalise(r["pattern"]) == desc_norm:
                if r.get("coa_code") == coa_code and _rule_direction(r) != direction:
                    direction = DIRECTION_ANY
                r["coa_code"] = coa_code
                r["coa_name"] = coa_name
                r["is_transfer"] = is_transfer
                r["direction"] = direction
                r["confirmed_count"] = r.get("confirmed_count", 1) + 1
                self._save()
                return
        self._rules.append({
            "pattern": safe_description,
            "coa_code": coa_code,
            "coa_name": coa_name,
            "is_transfer": is_transfer,
            "confirmed_count": 1,
            "direction": direction,
        })
        self._save()

    def list_rules(self) -> list[dict]:
        return list(self._rules)

    def remove_rule(self, pattern: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules
                       if self._normalise(r["pattern"]) != self._normalise(pattern)]
        if len(self._rules) < before:
            self._save()
            return True
        return False

    def _load(self) -> None:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self._rules = self._upgrade_legacy_seeds(data.get("rules", []))
            except Exception:
                self._rules = []
        else:
            self._rules = []
            # Pre-seed with known patterns from the sample statements
            self._seed_defaults()

    @classmethod
    def _upgrade_legacy_seeds(cls, rules: list) -> list[dict]:
        """C-14: tighten or drop unconfirmed seeds written by earlier versions."""
        upgraded: list[dict] = []
        for r in rules:
            if not isinstance(r, dict):
                continue
            if not r.get("confirmed_count"):
                key = (cls._normalise(str(r.get("pattern", ""))), str(r.get("coa_code", "")))
                if key in _LEGACY_SEEDS_DROPPED:
                    continue
                if key in _LEGACY_SEED_UPGRADES:
                    r = {**r, **_LEGACY_SEED_UPGRADES[key]}
            upgraded.append(r)
        return upgraded

    def _seed_defaults(self) -> None:
        # Intra-bank transfer keywords are institution-specific and live in
        # private/institutions.py (gitignored). Fall back to generic-only.
        try:
            from private.institutions import BANK_X
            _intra_xfer_keywords = BANK_X.get("intra_xfer_keywords", [])
        except ImportError:
            _intra_xfer_keywords = []

        # (pattern, code, name, is_transfer, direction, match)
        defaults = [
            # Software — canonical 5010
            ("INCFILE LLC", "5071", "Legal & Professional Fees", False, DIRECTION_ANY, None),
            ("PAYPAL *QUICKBOOKS", "5010", "Software & Subscriptions", False,
             DIRECTION_ANY, None),
            ("GOOGLE", "5010", "Software & Subscriptions", False, DIRECTION_ANY, None),
            # V7 fix: estimated-tax payments are partner draws, not entity expense
            ("USATAXPYMT IRS", "3040", "Members Distributions / Owner Draws", False,
             DIRECTION_OUT, None),
            # Payroll tax — canonical 5040
            ("TAX PAYROLL", "5040", "Payroll Tax Expense", False, DIRECTION_ANY, None),
            # Bank fees — canonical 5020
            ("TRAN FEE INTUIT", "5020", "Bank & Transaction Fees", False, DIRECTION_ANY, None),
            # Transfers — canonical 9000 (is_transfer=True excluded from P&L)
            # Institution-specific intra-bank keywords loaded from private config.
            # C-14: replayed only on a row with an own-account cue (see lookup).
            # ("EFT FUNDS PAID" → 9000 was removed: wording only.)
            *[(kw, "9000", "Inter-Account Transfer (Clearing)", True, DIRECTION_ANY, None)
              for kw in _intra_xfer_keywords],
            # Service revenue — canonical 4020.  C-14: inflows only, whole words only
            # (it used to fuzzy-match any "... DEPOSIT").
            ("DEPOSIT INTUIT", "4020", "Service Revenue", False, DIRECTION_IN, MATCH_WORD),
            # Margin interest — canonical 5030
            ("MARGIN INTEREST", "5030", "Margin Interest Expense", False, DIRECTION_ANY, None),
        ]
        for pattern, code, name, is_xfer, direction, match in defaults:
            rule = {
                "pattern": pattern,
                "coa_code": code,
                "coa_name": name,
                "is_transfer": is_xfer,
                "confirmed_count": 0,  # 0 = seeded, not user-confirmed
                "direction": direction,
            }
            if match:
                rule["match"] = match
            self._rules.append(rule)
        self._save()

    def _save(self) -> None:
        """Atomically rewrite the memory file.

        Writes to a temp file in the same directory and ``os.replace``s it over
        the target, so a crash mid-write never leaves truncated JSON behind
        (``os.replace`` is atomic on both POSIX and Windows).
        """
        self._file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"rules": self._rules}, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._file.name}.", suffix=".tmp", dir=str(self._file.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._file)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _normalise(text: str) -> str:
        # Upper-case, collapse spaces, strip date tokens like "01-08"
        text = text.upper().strip()
        text = re.sub(r"\b\d{2}-\d{2}\b", "", text)  # remove MM-DD
        text = re.sub(r"\b\d{6,}\b", "", text)  # remove long numbers
        text = re.sub(r"\s+", " ", text).strip()
        return text


# Module-level singleton
_memory: ClassificationMemory | None = None


def get_memory() -> ClassificationMemory:
    global _memory
    if _memory is None:
        _memory = ClassificationMemory()
    return _memory

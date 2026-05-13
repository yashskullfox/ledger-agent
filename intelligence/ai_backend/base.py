"""
intelligence/ai_backend/base.py  –  Abstract base class for AI backends
────────────────────────────────────────────────────────────────────────
Every backend must implement three methods:

  classify_transaction(description, amount, context)
      → {"coa_code": str, "coa_name": str, "is_transfer": bool,
          "confidence": float, "reason": str}

  enhance_memory_rule(pattern, coa_code, confirmed_count)
      → {"enhanced_pattern": str, "suggested_aliases": List[str]}

  explain_classification(description, coa_code, coa_name)
      → str  (human-readable explanation)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class AIBackend(ABC):
    """Abstract AI backend interface."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Short identifier e.g. 'local', 'openai', 'gemini'."""

    @abstractmethod
    def classify_transaction(
            self,
            description: str,
            amount: float,
            context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Suggest a COA classification for a transaction.

        Args:
            description: Cleaned transaction description text.
            amount: Transaction amount (negative = debit, positive = credit).
            context: Optional dict with extra context:
                     {"coa_entries": [...], "entity_name": str, ...}

        Returns:
            {
                "coa_code":   str,   # e.g. "5010"
                "coa_name":   str,   # e.g. "Software & SaaS Subscriptions"
                "is_transfer": bool,
                "confidence": float, # 0.0 – 1.0
                "reason":     str,   # human-readable reasoning
            }
        """

    @abstractmethod
    def enhance_memory_rule(
            self,
            pattern: str,
            coa_code: str,
            confirmed_count: int,
    ) -> Dict[str, Any]:
        """
        Suggest improvements to an existing classification memory rule.

        Returns:
            {
                "enhanced_pattern":    str,
                "suggested_aliases":   List[str],
                "confidence":          float,
            }
        """

    @abstractmethod
    def explain_classification(
            self,
            description: str,
            coa_code: str,
            coa_name: str,
    ) -> str:
        """Return a human-readable explanation for why this classification fits."""

    # ── Optional hook ─────────────────────────────────────────────────────────

    def on_user_confirmed(
            self,
            description: str,
            coa_code: str,
            coa_name: str,
            is_transfer: bool,
    ) -> None:
        """
        Called when the user confirms a classification interactively.
        Default: no-op.  Backends may use this to track learning signals.
        """

"""
intelligence/ai_backend/chained_backend.py  –  Local-first AI backend chain

Implements the "local compute first" principle:

  1. Always runs the local rule engine at full capacity (zero API cost)
  2. Only escalates to the remote AI backend (OpenAI / Gemini) when local
     confidence falls below LOCAL_CONFIDENCE_THRESHOLD
  3. Returns the highest-confidence result; gracefully degrades to local
     if the remote call fails, is unavailable, or is not configured

This backend is selected automatically when FI_AI_BACKEND=openai or gemini
is set. It wraps the remote backend — the caller never needs to manage the
chain explicitly.

Environment variables:
  FI_AI_BACKEND                  local | openai | gemini (default: local)
  FI_LOCAL_CONFIDENCE_THRESHOLD  0.0–1.0, default 0.65
    Minimum confidence the local backend must reach before the remote
    AI backend is skipped. Below this threshold, the chain tries the
    remote backend and returns whichever gives higher confidence.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from config import LOCAL_CONFIDENCE_THRESHOLD as _DEFAULT_THRESHOLD
from ledger_agent.core.intelligence.ai_backend.base import AIBackend
from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend


class ChainedBackend(AIBackend):
    """
    Local-first backend that escalates to a remote AI only for low-confidence
    classifications.  This keeps API costs near zero for well-known vendors
    while still benefiting from AI intelligence on unusual transactions.
    """

    def __init__(self, remote: AIBackend, threshold: float = _DEFAULT_THRESHOLD):
        self._local = LocalBackend()
        self._remote = remote
        self._threshold = threshold

    @property
    def backend_name(self) -> str:
        return f"chained(local→{self._remote.backend_name})"

    def classify_transaction(
            self,
            description: str,
            amount: float,
            context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        local_result = self._local.classify_transaction(description, amount, context)
        if local_result.get("confidence", 0.0) >= self._threshold:
            local_result["source"] = "local"
            return local_result

        try:
            remote_result = self._remote.classify_transaction(description, amount, context)
            remote_result["source"] = self._remote.backend_name
            if remote_result.get("confidence", 0.0) >= local_result.get("confidence", 0.0):
                return remote_result
        except Exception:
            pass

        local_result["source"] = "local_fallback"
        return local_result

    def enhance_memory_rule(
            self,
            pattern: str,
            coa_code: str,
            confirmed_count: int,
    ) -> Dict[str, Any]:
        try:
            return self._remote.enhance_memory_rule(pattern, coa_code, confirmed_count)
        except Exception:
            return self._local.enhance_memory_rule(pattern, coa_code, confirmed_count)

    def explain_classification(
            self,
            description: str,
            coa_code: str,
            coa_name: str,
    ) -> str:
        try:
            return self._remote.explain_classification(description, coa_code, coa_name)
        except Exception:
            return self._local.explain_classification(description, coa_code, coa_name)

    def on_user_confirmed(
            self,
            description: str,
            coa_code: str,
            coa_name: str,
            is_transfer: bool,
    ) -> None:
        self._local.on_user_confirmed(description, coa_code, coa_name, is_transfer)
        try:
            self._remote.on_user_confirmed(description, coa_code, coa_name, is_transfer)
        except Exception:
            pass

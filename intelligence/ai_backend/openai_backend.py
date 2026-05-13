"""
intelligence/ai_backend/openai_backend.py  –  OpenAI Chat Completions backend
───────────────────────────────────────────────────────────────────────────────
Requires:  pip install openai tenacity  (or: pip install financial-intelligence[openai])
Env vars:
  FI_OPENAI_API_KEY   Your OpenAI API key
  FI_OPENAI_MODEL     Model name (default: gpt-4o-mini)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from intelligence.ai_backend.base import AIBackend

log = get_logger(__name__)

_SYSTEM_PROMPT = """You are an expert small-business bookkeeper.
Given a bank/brokerage transaction description and amount, classify it to the most
appropriate Chart of Accounts (COA) code.

Respond ONLY with valid JSON in this exact format:
{
  "coa_code": "<4-digit code>",
  "coa_name": "<account name>",
  "is_transfer": <true|false>,
  "confidence": <0.0-1.0>,
  "reason": "<brief explanation>"
}

Common COA codes:
4000=General Revenue, 4010=Realized Gains/Losses, 4020=Dividend Income,
4030=Interest Income, 5010=Software & SaaS, 5020=Payroll & Wages,
5030=Advertising & Marketing, 5040=Estimated Tax Payments, 5050=Federal Income Tax,
5055=State & Local Taxes, 5060=Office Supplies, 5070=Legal & Professional,
5080=Bank Fees, 5090=Interest Expense, 5100=Travel & Transportation,
5999=Uncategorized Expense, 9000=Inter-Account Transfer
"""

class OpenAIBackend(AIBackend):
    """OpenAI Chat Completions with retry logic and 30-second timeout."""

    def __init__(self) -> None:
        try:
            import openai
        except ImportError:
            raise ImportError(
                "openai package not installed. Run: pip install financial-intelligence[openai]"
            )
        from config import ai_api_key, OPENAI_MODEL
        self._client = openai.OpenAI(api_key=ai_api_key(), timeout=30.0)
        self._model = OPENAI_MODEL
        log.info("OpenAI backend initialised", extra={"model": self._model})

    @property
    def backend_name(self) -> str:
        return "openai"

    def _chat(self, messages: List[Dict], retries: int = 3) -> str:
        """Call OpenAI with exponential-backoff retries via tenacity."""
        try:
            from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
            import openai as _openai

            @retry(
                stop=stop_after_attempt(retries),
                wait=wait_exponential(multiplier=1, min=2, max=30),
                retry=retry_if_exception_type((_openai.RateLimitError, _openai.APITimeoutError)),
            )
            def _call() -> str:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=256,
                )
                return resp.choices[0].message.content or ""

            return _call()
        except Exception as exc:
            log.warning("OpenAI call failed", extra={"error": str(exc)})
            raise

    def classify_transaction(
            self,
            description: str,
            amount: float,
            context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        direction = "credit (income)" if amount >= 0 else "debit (expense)"
        user_msg = (
            f"Transaction: '{description}'\n"
            f"Amount: ${abs(amount):,.2f} ({direction})\n"
            f"Entity: {(context or {}).get('entity_name', 'LLC')}\n"
            "Classify this transaction."
        )
        try:
            raw = self._chat([
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ])
            result = json.loads(raw)
            result.setdefault("confidence", 0.85)
            return result
        except Exception as exc:
            log.warning("OpenAI classify failed, falling back to local", extra={"error": str(exc)})
            from intelligence.ai_backend.local_backend import LocalBackend
            return LocalBackend().classify_transaction(description, amount, context)

    def enhance_memory_rule(
            self,
            pattern: str,
            coa_code: str,
            confirmed_count: int,
    ) -> Dict[str, Any]:
        user_msg = (
            f"Pattern: '{pattern}'\nCOA code: {coa_code}\n"
            f"Confirmed {confirmed_count} times.\n"
            "Suggest an enhanced pattern and up to 3 aliases. "
            "Respond as JSON: {enhanced_pattern, suggested_aliases, confidence}"
        )
        try:
            raw = self._chat([
                {"role": "system", "content": "You are an expert at financial transaction pattern matching."},
                {"role": "user", "content": user_msg},
            ])
            return json.loads(raw)
        except Exception:
            from intelligence.ai_backend.local_backend import LocalBackend
            return LocalBackend().enhance_memory_rule(pattern, coa_code, confirmed_count)

    def explain_classification(
            self,
            description: str,
            coa_code: str,
            coa_name: str,
    ) -> str:
        user_msg = (
            f"Why is transaction '{description}' classified as "
            f"[{coa_code}] {coa_name}? Give a 1-2 sentence explanation."
        )
        try:
            return self._chat([
                {"role": "system", "content": "You are a bookkeeper. Be concise."},
                {"role": "user", "content": user_msg},
            ])
        except Exception:
            return f"Transaction classified as [{coa_code}] {coa_name} via OpenAI analysis."

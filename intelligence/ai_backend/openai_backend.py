"""
intelligence/ai_backend/openai_backend.py  –  OpenAI Chat Completions backend
───────────────────────────────────────────────────────────────────────────────
Requires:  pip install openai tenacity  (or: pip install financial-intelligence[openai])
Env vars:
  FI_OPENAI_API_KEY   Your OpenAI API key
  FI_OPENAI_MODEL     Model name (default: gpt-4o-mini)

Privacy (R-46):
  All outbound payloads are run through core.privacy.redact() before sending.
  core.privacy.audit_egress() is called immediately before every HTTP request
  as a hard pre-flight check.  Set FI_AI_EGRESS_MODE=mock to disable network
  calls entirely (CI / offline environments).
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

Common COA codes (canonical — use these exact codes and names):
4010=Realised Trading Gains, 4020=Service Revenue, 4021=Dividend Income,
4031=Interest Income, 5010=Software & Subscriptions, 5020=Bank & Transaction Fees,
5021=Payroll & Wages, 5030=Margin Interest Expense, 5031=Advertising & Marketing,
5040=Payroll Tax Expense, 5050=Federal Income Tax Expense, 5055=State & Local Taxes,
5061=Office & Shipping Supplies, 5071=Legal & Professional Fees, 5080=Other Operating Expenses,
5090=Interest Expense, 5100=Travel & Transportation,
5999=Uncategorized Expense, 9000=Inter-Account Transfer
"""

_MOCK_CLASSIFICATION: Dict[str, Any] = {
    "coa_code": "5999",
    "coa_name": "Uncategorized Expense",
    "is_transfer": False,
    "confidence": 0.50,
    "source": "mock",
    "reason": "Mock mode — no remote call made.",
}


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
            from core.privacy import audit_egress

            # Hard pre-flight PII audit before any HTTP call
            audit_egress(messages)

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
        from config import AI_EGRESS_MODE
        from core.privacy import redact, unredact_result

        # Mock mode — no network call
        if AI_EGRESS_MODE == "mock":
            log.info("OpenAI mock mode: skipping remote call")
            return dict(_MOCK_CLASSIFICATION)

        # Redact PII before sending
        entity_name = (context or {}).get("entity_name", "LLC")
        safe_desc, m1 = redact(description, scope="openai")
        safe_entity, m2 = redact(entity_name, scope="openai")
        combined_map = {**m1, **m2}

        direction = "credit (income)" if amount >= 0 else "debit (expense)"
        user_msg = (
            f"Transaction: '{safe_desc}'\n"
            f"Amount: ${abs(amount):,.2f} ({direction})\n"
            f"Entity: {safe_entity}\n"
            "Classify this transaction."
        )
        try:
            raw = self._chat([
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ])
            result = json.loads(raw)
            result.setdefault("confidence", 0.85)
            # Un-redact the reason field for local display
            return unredact_result(result, combined_map)
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
        from config import AI_EGRESS_MODE
        from core.privacy import redact

        if AI_EGRESS_MODE == "mock":
            return {"enhanced_pattern": pattern, "suggested_aliases": [], "confidence": 0.50, "source": "mock"}

        safe_pattern, _ = redact(pattern, scope="openai")
        user_msg = (
            f"Pattern: '{safe_pattern}'\nCOA code: {coa_code}\n"
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
        from config import AI_EGRESS_MODE
        from core.privacy import redact, unredact

        if AI_EGRESS_MODE == "mock":
            return f"Transaction classified as [{coa_code}] {coa_name} (mock mode)."

        safe_desc, mapping = redact(description, scope="openai")
        user_msg = (
            f"Why is transaction '{safe_desc}' classified as "
            f"[{coa_code}] {coa_name}? Give a 1-2 sentence explanation."
        )
        try:
            raw = self._chat([
                {"role": "system", "content": "You are a bookkeeper. Be concise."},
                {"role": "user", "content": user_msg},
            ])
            return unredact(raw, mapping)
        except Exception:
            return f"Transaction classified as [{coa_code}] {coa_name} via OpenAI analysis."

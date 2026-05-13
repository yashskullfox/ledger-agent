"""
intelligence/ai_backend/gemini_backend.py  –  Google Gemini backend
─────────────────────────────────────────────────────────────────────
Requires:  pip install google-generativeai tenacity
           (or: pip install financial-intelligence[gemini])
Env vars:
  FI_GEMINI_API_KEY   Your Google AI API key
  FI_GEMINI_MODEL     Model name (default: gemini-1.5-flash)
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from core.logging_setup import get_logger
from intelligence.ai_backend.base import AIBackend

log = get_logger(__name__)

_SYSTEM_INSTRUCTION = (
    "You are an expert small-business bookkeeper. "
    "Classify bank/brokerage transactions to Chart of Accounts codes. "
    "Always respond with valid JSON only — no markdown, no extra text."
)

_COA_HINT = (
    "COA(canonical): 4010=Realised Trading Gains,4020=Service Revenue,"
    "4021=Dividend Income,4031=Interest Income,"
    "5010=Software & Subscriptions,5020=Bank & Transaction Fees,"
    "5021=Payroll & Wages,5030=Margin Interest Expense,"
    "5031=Advertising & Marketing,5040=Payroll Tax Expense,"
    "5050=Federal Income Tax Expense,5055=State & Local Taxes,"
    "5061=Office & Shipping Supplies,5071=Legal & Professional Fees,"
    "5080=Other Operating Expenses,5090=Interest Expense,"
    "5100=Travel & Transportation,5999=Uncategorized Expense,9000=Inter-Account Transfer"
)


class GeminiBackend(AIBackend):
    """Google Gemini backend with graceful local fallback."""

    def __init__(self) -> None:
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError(
                "google-generativeai not installed. "
                "Run: pip install financial-intelligence[gemini]"
            )
        from config import ai_api_key, GEMINI_MODEL
        genai.configure(api_key=ai_api_key())
        self._model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=_SYSTEM_INSTRUCTION,
        )
        self._model_name = GEMINI_MODEL
        log.info("Gemini backend initialised", extra={"model": GEMINI_MODEL})

    @property
    def backend_name(self) -> str:
        return "gemini"

    def _generate(self, prompt: str) -> str:
        """Send a prompt and return text response with basic retry."""
        try:
            from tenacity import retry, stop_after_attempt, wait_exponential

            @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
            def _call() -> str:
                resp = self._model.generate_content(prompt)
                return resp.text or ""

            return _call()
        except Exception as exc:
            log.warning("Gemini call failed", extra={"error": str(exc)})
            raise

    def _parse_json(self, raw: str) -> Dict[str, Any]:
        """Extract JSON from Gemini response (which may include markdown fences)."""
        # Strip ```json ... ``` fences if present
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        return json.loads(raw)

    def classify_transaction(
            self,
            description: str,
            amount: float,
            context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        direction = "credit/income" if amount >= 0 else "debit/expense"
        prompt = (
            f"Classify this transaction for a small business LLC.\n"
            f"Description: {description}\n"
            f"Amount: ${abs(amount):,.2f} ({direction})\n"
            f"{_COA_HINT}\n"
            f'Respond ONLY with JSON: {{"coa_code":"","coa_name":"","is_transfer":false,"confidence":0.85,"reason":""}}'
        )
        try:
            raw = self._generate(prompt)
            result = self._parse_json(raw)
            result.setdefault("confidence", 0.80)
            return result
        except Exception as exc:
            log.warning("Gemini classify failed, using local fallback", extra={"error": str(exc)})
            from intelligence.ai_backend.local_backend import LocalBackend
            return LocalBackend().classify_transaction(description, amount, context)

    def enhance_memory_rule(
            self,
            pattern: str,
            coa_code: str,
            confirmed_count: int,
    ) -> Dict[str, Any]:
        prompt = (
            f"Enhance this classification pattern for a financial transaction classifier.\n"
            f"Pattern: '{pattern}', COA code: {coa_code}, confirmed {confirmed_count} times.\n"
            f'Respond ONLY with JSON: {{"enhanced_pattern":"","suggested_aliases":[],"confidence":0.8}}'
        )
        try:
            raw = self._generate(prompt)
            return self._parse_json(raw)
        except Exception:
            from intelligence.ai_backend.local_backend import LocalBackend
            return LocalBackend().enhance_memory_rule(pattern, coa_code, confirmed_count)

    def explain_classification(
            self,
            description: str,
            coa_code: str,
            coa_name: str,
    ) -> str:
        prompt = (
            f"In 1-2 sentences, explain why '{description}' is classified "
            f"as [{coa_code}] {coa_name} for a small business."
        )
        try:
            return self._generate(prompt).strip()
        except Exception:
            return f"Transaction classified as [{coa_code}] {coa_name} via Gemini AI."

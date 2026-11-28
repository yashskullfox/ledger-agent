"""
intelligence/ai_backend/_remote.py  –  Shared code path for remote AI backends
──────────────────────────────────────────────────────────────────────────────
Every remote provider (OpenAI, Gemini, Anthropic, OpenAI-compatible / Ollama)
subclasses ``RemoteAIBackend`` and implements only two provider-specific hooks:

  _build_payload(system, user) → the exact object that will be sent
  _send(payload)               → raw text returned by the provider

Everything privacy-relevant lives here, once, so a new provider cannot skip it:

  1. FI_AI_EGRESS_MODE=mock short-circuits before anything is built or sent.
  2. Free-text inputs (description / pattern) go through privacy.redact()
     with the backend's scope before they are placed in a prompt.
  3. privacy.audit_egress(payload) runs on the exact outbound payload
     immediately before _send(); a PrivacyLeakError blocks the call.
  4. Any exception (missing SDK, network, audit failure, malformed / invalid
     response) falls back to the LocalBackend — fail closed, never crash.

Responses are parsed with ``extract_json`` (tolerates ``` fences and prose
around the object) and validated with ``validate_classification`` (coa_code
must be a known COA code string; confidence clamped to 0..1).

The prompts carry no entity, partner, or account data, and are
language-agnostic: descriptions may be in any language or script and are
classified by meaning into fixed English COA codes.  Nothing is translated or
added beyond the redacted description, the amount, and its direction.

This module performs no network I/O itself and imports no HTTP library
(core purity, ARCH-02); provider SDKs are imported lazily by subclasses.
"""
from __future__ import annotations

import json
import math
import re
from abc import abstractmethod
from collections.abc import Callable, Iterable
from typing import Any

from ledger_agent.core.intelligence.ai_backend.base import AIBackend
from ledger_agent.core.logging_setup import get_logger

log = get_logger(__name__)

# ── Prompts ───────────────────────────────────────────────────────────────────

# Canonical subset shown to the model (codes and names match _DEFAULT_COA in
# core/database.py).  Validation accepts every code in _DEFAULT_COA.
COA_PROMPT_HINT = (
    "4010=Realised Trading Gains, 4020=Service Revenue, 4021=Dividend Income, "
    "4031=Interest Income, 5010=Software & Subscriptions, 5020=Bank & Transaction Fees, "
    "5021=Payroll & Wages, 5030=Margin Interest Expense, 5031=Advertising & Marketing, "
    "5040=Payroll Tax Expense, 5050=Federal Income Tax Expense, 5055=State & Local Taxes, "
    "5061=Office & Shipping Supplies, 5071=Legal & Professional Fees, "
    "5080=Other Operating Expenses, 5090=Interest Expense, 5100=Travel & Transportation, "
    "5999=Uncategorized Expense, 9000=Inter-Account Transfer"
)

CLASSIFY_SYSTEM_PROMPT = f"""You are an expert small-business bookkeeper.
Given one bank or brokerage transaction description and its amount, classify it to the
most appropriate Chart of Accounts (COA) code.

The description may be written in any language or script (for example English, German,
Japanese, Spanish, Hindi, Chinese) and may mix languages, abbreviations, or merchant
codes. Classify by meaning, not by surface wording. Do not translate the description in
your answer.

Tokens in angle brackets (for example <COUNTERPARTY_001>) are privacy placeholders for
removed values; treat them as opaque.

Respond ONLY with one JSON object, no markdown and no extra text, in exactly this shape:
{{"coa_code": "<4-digit code>", "coa_name": "<account name>", "is_transfer": <true|false>,
"confidence": <0.0-1.0>, "reason": "<brief explanation in English>"}}

coa_code and coa_name must be copied exactly (in English) from this list:
{COA_PROMPT_HINT}
If nothing fits, use 5999 (expense) or 4020 (income) with low confidence."""

ENHANCE_SYSTEM_PROMPT = (
    "You are an expert at financial transaction pattern matching. Patterns may be in any "
    "language or script. Respond ONLY with JSON: "
    '{"enhanced_pattern": "", "suggested_aliases": [], "confidence": 0.8}'
)

EXPLAIN_SYSTEM_PROMPT = (
    "You are a bookkeeper. Be concise. The transaction description may be in any "
    "language or script; answer in English."
)

MOCK_CLASSIFICATION: dict[str, Any] = {
    "coa_code": "5999",
    "coa_name": "Uncategorized Expense",
    "is_transfer": False,
    "confidence": 0.50,
    "source": "mock",
    "reason": "Mock mode — no remote call made.",
}


def build_classify_user_message(safe_description: str, amount: float) -> str:
    """User turn for classification. Takes an ALREADY-REDACTED description."""
    direction = "credit (income)" if amount >= 0 else "debit (expense)"
    return (
        f"Transaction description: {safe_description}\n"
        f"Amount: {abs(amount):,.2f} ({direction})\n"
        "Classify this transaction."
    )


# ── JSON extraction / validation ──────────────────────────────────────────────

_FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*\s*(.*?)```", re.DOTALL)


def extract_json(raw: str) -> dict[str, Any]:
    """
    Parse the first JSON object in a model response.

    Handles ```json fences, leading/trailing prose, and nested braces.
    Raises ValueError when no JSON object can be decoded.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty model response")
    text = raw.strip()

    candidates: list[str] = [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    candidates.append(text)

    decoder = json.JSONDecoder()
    for cand in candidates:
        start = cand.find("{")
        while start != -1:
            try:
                obj, _ = decoder.raw_decode(cand, start)
            except json.JSONDecodeError:
                start = cand.find("{", start + 1)
                continue
            if isinstance(obj, dict):
                return obj
            start = cand.find("{", start + 1)
    raise ValueError("no JSON object found in model response")


def known_coa_codes(context: dict[str, Any] | None = None) -> dict[str, str]:
    """Return {code: name} for every valid COA code (defaults + context entries)."""
    from ledger_agent.core.database import _DEFAULT_COA

    codes = {str(row[0]): str(row[1]) for row in _DEFAULT_COA}
    for entry in (context or {}).get("coa_entries") or []:
        code = getattr(entry, "code", None)
        if code is None and isinstance(entry, dict):
            code = entry.get("code")
        name = getattr(entry, "name", None)
        if name is None and isinstance(entry, dict):
            name = entry.get("name")
        if code:
            codes[str(code)] = str(name or codes.get(str(code), ""))
    return codes


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    return bool(value)


def validate_classification(
        result: dict[str, Any],
        default_confidence: float,
        context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Normalise and validate a classification dict from a remote model.

    Raises ValueError if coa_code is missing or not a known COA code, or if
    confidence is not numeric (NaN and +/-inf count as non-numeric).
    Confidence is clamped to [0.0, 1.0].
    """
    if not isinstance(result, dict):
        raise ValueError("classification is not a JSON object")

    raw_code = result.get("coa_code")
    if isinstance(raw_code, bool) or raw_code is None:
        raise ValueError("coa_code missing")
    if isinstance(raw_code, int):
        raw_code = str(raw_code)
    if not isinstance(raw_code, str):
        raise ValueError("coa_code must be a string")
    code = raw_code.strip()

    codes = known_coa_codes(context)
    if code not in codes:
        raise ValueError("coa_code is not a known COA code")

    conf_raw = result.get("confidence", default_confidence)
    if isinstance(conf_raw, bool):
        raise ValueError("confidence must be numeric")
    try:
        confidence = float(conf_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be numeric") from exc
    if math.isnan(confidence) or math.isinf(confidence):
        raise ValueError("confidence must be numeric")
    confidence = max(0.0, min(1.0, confidence))

    name = result.get("coa_name")
    if not isinstance(name, str) or not name.strip():
        name = codes[code]

    reason = result.get("reason", "")
    out = dict(result)
    out.update({
        "coa_code": code,
        "coa_name": name.strip(),
        "is_transfer": _as_bool(result.get("is_transfer", False)),
        "confidence": confidence,
        "reason": reason if isinstance(reason, str) else str(reason),
    })
    return out


# ── Retry helper (tenacity optional) ──────────────────────────────────────────

def call_with_retries(
        fn: Callable[[], str],
        retry_on: Iterable[type[BaseException]] = (),
        attempts: int = 3,
        max_wait: float = 30.0,
) -> str:
    """
    Run ``fn`` with exponential-backoff retries when tenacity is installed.
    Without tenacity, ``fn`` is called once.  ``retry_on`` empty → retry on
    any Exception (Gemini's historic behaviour).
    """
    try:
        from tenacity import (
            retry,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )
    except ImportError:
        return fn()

    exc_types = tuple(retry_on) or (Exception,)
    wrapped = retry(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=1, min=2, max=max_wait),
        retry=retry_if_exception_type(exc_types),
        reraise=True,
    )(fn)
    return wrapped()


# ── Base class ────────────────────────────────────────────────────────────────

class RemoteAIBackend(AIBackend):
    """
    Shared mock/redact/audit/parse/validate/fallback pipeline for remote models.

    Subclasses set ``_scope`` (privacy.redact scope), ``_label`` (human name),
    ``_default_confidence`` and implement ``_build_payload`` / ``_send``.
    """

    _scope: str = "ai_context"
    _label: str = "remote AI"
    _default_confidence: float = 0.85

    # ── provider hooks ──────────────────────────────────────────────────────

    @abstractmethod
    def _build_payload(self, system: str, user: str) -> Any:
        """Return the exact provider payload that will be sent (audited verbatim)."""

    @abstractmethod
    def _send(self, payload: Any) -> str:
        """Send ``payload`` to the provider and return the raw text response."""

    # ── shared pipeline ─────────────────────────────────────────────────────

    @staticmethod
    def _egress_mode() -> str:
        from ledger_agent.core.config import AI_EGRESS_MODE  # read at call time (tests mutate it)
        return AI_EGRESS_MODE

    def _redact(self, text: str) -> tuple[str, dict[str, str]]:
        from ledger_agent.core.privacy import redact
        return redact(text, scope=self._scope)

    def _request(self, system: str, user: str) -> str:
        """Build payload → audit_egress (hard pre-flight) → send."""
        from ledger_agent.core.privacy import audit_egress

        payload = self._build_payload(system, user)
        audit_egress(payload)
        try:
            return self._send(payload)
        except Exception as exc:
            log.warning(f"{self._label} call failed", extra={"error": type(exc).__name__})
            raise

    @staticmethod
    def _local():
        from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
        return LocalBackend()

    def classify_transaction(
            self,
            description: str,
            amount: float,
            context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._egress_mode() == "mock":
            log.info(f"{self._label} mock mode: skipping remote call")
            return dict(MOCK_CLASSIFICATION)

        try:
            from ledger_agent.core.privacy import unredact_result

            safe_desc, mapping = self._redact(description)
            user_msg = build_classify_user_message(safe_desc, amount)
            raw = self._request(CLASSIFY_SYSTEM_PROMPT, user_msg)
            result = validate_classification(
                extract_json(raw), self._default_confidence, context
            )
            return unredact_result(result, mapping)
        except Exception as exc:
            log.warning(
                f"{self._label} classify failed, falling back to local",
                extra={"error": type(exc).__name__},
            )
            return self._local().classify_transaction(description, amount, context)

    def enhance_memory_rule(
            self,
            pattern: str,
            coa_code: str,
            confirmed_count: int,
    ) -> dict[str, Any]:
        if self._egress_mode() == "mock":
            return {
                "enhanced_pattern": pattern,
                "suggested_aliases": [],
                "confidence": 0.50,
                "source": "mock",
            }
        try:
            safe_pattern, _ = self._redact(pattern)
            user_msg = (
                f"Pattern: '{safe_pattern}'\nCOA code: {coa_code}\n"
                f"Confirmed {confirmed_count} times.\n"
                "Suggest an enhanced pattern and up to 3 aliases."
            )
            return extract_json(self._request(ENHANCE_SYSTEM_PROMPT, user_msg))
        except Exception:
            return self._local().enhance_memory_rule(pattern, coa_code, confirmed_count)

    def explain_classification(
            self,
            description: str,
            coa_code: str,
            coa_name: str,
    ) -> str:
        if self._egress_mode() == "mock":
            return f"Transaction classified as [{coa_code}] {coa_name} (mock mode)."
        try:
            from ledger_agent.core.privacy import unredact

            safe_desc, mapping = self._redact(description)
            user_msg = (
                f"Why is transaction '{safe_desc}' classified as "
                f"[{coa_code}] {coa_name}? Give a 1-2 sentence explanation."
            )
            return unredact(self._request(EXPLAIN_SYSTEM_PROMPT, user_msg).strip(), mapping)
        except Exception:
            return f"Transaction classified as [{coa_code}] {coa_name} via {self._label}."

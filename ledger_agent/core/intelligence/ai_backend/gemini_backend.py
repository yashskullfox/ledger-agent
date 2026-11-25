"""
intelligence/ai_backend/gemini_backend.py  –  Google Gemini backend
─────────────────────────────────────────────────────────────────────
Requires:  pip install google-generativeai tenacity
           (or: pip install ledger-agent[gemini])
Env vars:
  FI_GEMINI_API_KEY   Your Google AI API key
  FI_GEMINI_MODEL     Model name (default: gemini-1.5-flash)

Privacy (R-46):
  The shared RemoteAIBackend pipeline (_remote.py) redacts every outbound
  description with core.privacy.redact() and calls core.privacy.audit_egress()
  on the exact payload immediately before every generate call.
  Set FI_AI_EGRESS_MODE=mock to disable network calls (CI / offline use).
"""
from __future__ import annotations

from typing import Any

from ledger_agent.core.intelligence.ai_backend._remote import (
    RemoteAIBackend,
    call_with_retries,
    extract_json,
)
from ledger_agent.core.logging_setup import get_logger

log = get_logger(__name__)


class GeminiBackend(RemoteAIBackend):
    """Google Gemini backend with graceful local fallback."""

    _scope = "gemini"
    _label = "Gemini AI"
    _default_confidence = 0.80

    def __init__(self) -> None:
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise ImportError(
                "google-generativeai not installed. "
                "Run: pip install ledger-agent[gemini]"
            ) from exc
        from config import GEMINI_MODEL, ai_api_key
        genai.configure(api_key=ai_api_key())
        # The model object is bound to one system instruction, so the per-call
        # system prompt is folded into the content (see _build_payload).
        self._model = genai.GenerativeModel(model_name=GEMINI_MODEL)
        self._model_name = GEMINI_MODEL
        log.info("Gemini backend initialised", extra={"model": GEMINI_MODEL})

    @property
    def backend_name(self) -> str:
        return "gemini"

    def _build_payload(self, system: str, user: str) -> str:
        return f"{system}\n\n{user}"

    def _send(self, payload: Any) -> str:
        def _call() -> str:
            resp = self._model.generate_content(payload)
            return resp.text or ""

        return call_with_retries(_call, max_wait=20.0)

    def _parse_json(self, raw: str) -> dict[str, Any]:
        """Extract JSON from a Gemini response (which may include markdown fences)."""
        return extract_json(raw)


"""
intelligence/ai_backend/anthropic_backend.py  –  Anthropic Claude backend
──────────────────────────────────────────────────────────────────────────
Requires:  pip install anthropic  (or: pip install ledger-agent[anthropic])
Env vars:
  FI_ANTHROPIC_API_KEY   Anthropic API key (ANTHROPIC_API_KEY also accepted)
  FI_ANTHROPIC_MODEL     Model name (default: claude-haiku-5-5 — cheap/fast)
  FI_ANTHROPIC_EFFORT    output_config.effort sent with each request
                         (default: low; set to "none" to omit it, e.g. for
                         older models that do not accept effort)

Uses the official SDK's Messages API (messages.create with a system prompt and
one user message).  The SDK's own retry logic handles 408/409/429/5xx and
connection errors (max_retries=3); no temperature is sent because current
Claude models reject non-default sampling parameters.

Privacy (R-46):
  The shared RemoteAIBackend pipeline (_remote.py) redacts every outbound
  description with core.privacy.redact() and calls core.privacy.audit_egress()
  on the exact payload immediately before every request.
  Set FI_AI_EGRESS_MODE=mock to disable network calls entirely.
"""
from __future__ import annotations

from typing import Any

from ledger_agent.core.intelligence.ai_backend._remote import RemoteAIBackend
from ledger_agent.core.logging_setup import get_logger

log = get_logger(__name__)

# Room for adaptive thinking (on by default on current models) plus a short
# JSON answer.  Effort "low" keeps thinking minimal for classification.
_MAX_TOKENS = 1024


def _import_anthropic():
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "anthropic package not installed. Run: pip install ledger-agent[anthropic]"
        ) from exc
    return anthropic


class AnthropicBackend(RemoteAIBackend):
    """Anthropic Messages API backend with graceful local fallback."""

    _scope = "anthropic"
    _label = "Anthropic Claude"
    _default_confidence = 0.85

    def __init__(self) -> None:
        anthropic = _import_anthropic()
        from ledger_agent.core.config import ANTHROPIC_EFFORT, ANTHROPIC_MODEL, anthropic_api_key

        self._client = anthropic.Anthropic(
            api_key=anthropic_api_key(),
            timeout=30.0,
            max_retries=3,
        )
        self._model = ANTHROPIC_MODEL
        self._effort = ANTHROPIC_EFFORT
        log.info("Anthropic backend initialised", extra={"model": self._model})

    @property
    def backend_name(self) -> str:
        return "anthropic"

    def _build_payload(self, system: str, user: str) -> dict[str, Any]:
        return {
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

    def _send(self, payload: Any) -> str:
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _MAX_TOKENS,
            "system": payload["system"],
            "messages": payload["messages"],
        }
        effort = getattr(self, "_effort", "low")
        if effort and effort != "none":
            # Passed through extra_body so older SDK releases (which lack the
            # typed output_config kwarg) still send it.
            params["extra_body"] = {"output_config": {"effort": effort}}

        resp = self._client.messages.create(**params)

        if getattr(resp, "stop_reason", None) == "refusal":
            raise RuntimeError("model declined the request")
        texts = [
            getattr(block, "text", "")
            for block in (getattr(resp, "content", None) or [])
            if getattr(block, "type", None) == "text"
        ]
        return "".join(texts)

"""
intelligence/ai_backend/openai_backend.py  –  OpenAI Chat Completions backends
───────────────────────────────────────────────────────────────────────────────
Requires:  pip install openai tenacity  (or: pip install ledger-agent[openai])

Two backends share this module:

  OpenAIBackend            FI_AI_BACKEND=openai
    FI_OPENAI_API_KEY      Your OpenAI API key (required)
    FI_OPENAI_MODEL        Model name (default: gpt-4o-mini)
    FI_OPENAI_BASE_URL     Optional endpoint override (proxy / Azure-style gateway)

  OpenAICompatibleBackend  FI_AI_BACKEND=openai_compatible  (alias: ollama)
    Any server speaking the OpenAI Chat Completions protocol (Ollama, vLLM,
    LM Studio, llama.cpp server, …).
    FI_OPENAI_BASE_URL     Endpoint (default for ollama: http://localhost:11434/v1)
    FI_OPENAI_API_KEY      Optional; a placeholder is sent when unset
    FI_OPENAI_MODEL        Model name served by the endpoint

Privacy (R-46):
  The shared RemoteAIBackend pipeline (_remote.py) redacts every outbound
  description with core.privacy.redact() and calls core.privacy.audit_egress()
  on the exact payload immediately before every request.  This applies to
  local endpoints too — privacy is never relaxed because a URL looks local.
  Set FI_AI_EGRESS_MODE=mock to disable network calls entirely.
"""
from __future__ import annotations

from typing import Any

from ledger_agent.core.intelligence.ai_backend._remote import (
    RemoteAIBackend,
    call_with_retries,
)
from ledger_agent.core.logging_setup import get_logger

log = get_logger(__name__)

# Placeholder key for OpenAI-compatible servers that ignore authentication.
NO_KEY_PLACEHOLDER = "not-needed"


def _import_openai():
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "openai package not installed. Run: pip install ledger-agent[openai]"
        ) from exc
    return openai


class OpenAIBackend(RemoteAIBackend):
    """OpenAI Chat Completions with retry logic and 30-second timeout."""

    _scope = "openai"
    _label = "OpenAI"
    _default_confidence = 0.85
    # Newer OpenAI models reject max_tokens; older SDKs lack max_completion_tokens.
    _token_param = "max_completion_tokens"

    def __init__(self) -> None:
        openai = _import_openai()
        from config import OPENAI_BASE_URL, OPENAI_MODEL, ai_api_key

        kwargs: dict[str, Any] = {"api_key": ai_api_key(), "timeout": 30.0}
        if OPENAI_BASE_URL:
            kwargs["base_url"] = OPENAI_BASE_URL
        self._client = openai.OpenAI(**kwargs)
        self._model = OPENAI_MODEL
        log.info(f"{self._label} backend initialised", extra={"model": self._model})

    @property
    def backend_name(self) -> str:
        return "openai"

    def _build_payload(self, system: str, user: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _create(self, messages: list[dict[str, str]]) -> str:
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.1,
            self._token_param: 256,
        }
        try:
            resp = self._client.chat.completions.create(**params)
        except TypeError:
            if self._token_param == "max_tokens":
                raise
            # Older openai SDK without max_completion_tokens support.
            params["max_tokens"] = params.pop(self._token_param)
            resp = self._client.chat.completions.create(**params)
        return resp.choices[0].message.content or ""

    def _send(self, payload: Any) -> str:
        retry_on: tuple[type[BaseException], ...] = ()
        try:
            openai = _import_openai()
            retry_on = (openai.RateLimitError, openai.APITimeoutError)
        except (ImportError, AttributeError):
            pass
        if not retry_on:
            return self._create(payload)
        return call_with_retries(lambda: self._create(payload), retry_on=retry_on)


class OpenAICompatibleBackend(OpenAIBackend):
    """OpenAI SDK pointed at any OpenAI-compatible endpoint (Ollama, vLLM, …)."""

    _scope = "openai_compatible"
    _label = "OpenAI-compatible endpoint"
    _default_confidence = 0.80
    # Broadest compatibility across self-hosted servers.
    _token_param = "max_tokens"

    def __init__(self, default_base_url: str | None = None) -> None:
        openai = _import_openai()
        from config import (
            OLLAMA_DEFAULT_BASE_URL,
            OPENAI_BASE_URL,
            OPENAI_MODEL,
            openai_compatible_api_key,
        )

        base_url = OPENAI_BASE_URL or default_base_url or OLLAMA_DEFAULT_BASE_URL
        self._client = openai.OpenAI(
            api_key=openai_compatible_api_key() or NO_KEY_PLACEHOLDER,
            base_url=base_url,
            timeout=60.0,
        )
        self._model = OPENAI_MODEL
        log.info(f"{self._label} backend initialised", extra={"model": self._model})

    @property
    def backend_name(self) -> str:
        return "openai_compatible"

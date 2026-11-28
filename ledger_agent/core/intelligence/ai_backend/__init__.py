"""
intelligence/ai_backend/__init__.py  –  AI backend registry and factory

Local-first design principle
─────────────────────────────
The system maximises local CPU before touching any paid API:

  1. LocalBackend  – rule-based + rapidfuzz, zero API cost, works offline
  2. ChainedBackend – wraps LocalBackend + a remote AI backend; the remote
     is only called when local confidence < FI_LOCAL_CONFIDENCE_THRESHOLD
     (default 0.65).  This keeps API spend near zero for common vendors.
  3. MCP server (mcp_server/server.py) is completely optional and does NOT
     affect classification — it exposes finished results to MCP clients.

Selecting a backend (FI_AI_BACKEND env var):
  local              → LocalBackend only (default, no API key, works offline)
  openai             → ChainedBackend(local→OpenAI)
  gemini             → ChainedBackend(local→Gemini)
  anthropic          → ChainedBackend(local→Claude)
  openai_compatible  → ChainedBackend(local→any OpenAI-compatible endpoint at
                       FI_OPENAI_BASE_URL, e.g. vLLM / LM Studio / Ollama)
  ollama             → alias of openai_compatible, default base URL
                       http://localhost:11434/v1

Unknown values log a WARNING and fall back to local (never crash).
Every remote backend shares one privacy pipeline (_remote.py): redact() before
the prompt is built, audit_egress() right before the request, local fallback on
any failure.
"""
from __future__ import annotations

from collections.abc import Callable

from ledger_agent.core.intelligence.ai_backend.base import AIBackend
from ledger_agent.core.logging_setup import get_logger

log = get_logger(__name__)

_instance: AIBackend | None = None


# ── Factories (lazy imports: provider SDKs are optional) ──────────────────────

def _local() -> AIBackend:
    from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
    return LocalBackend()


def _chain(remote: AIBackend) -> AIBackend:
    from ledger_agent.core.intelligence.ai_backend.chained_backend import ChainedBackend
    return ChainedBackend(remote=remote)


def _openai() -> AIBackend:
    from ledger_agent.core.intelligence.ai_backend.openai_backend import OpenAIBackend
    return _chain(OpenAIBackend())


def _gemini() -> AIBackend:
    from ledger_agent.core.intelligence.ai_backend.gemini_backend import GeminiBackend
    return _chain(GeminiBackend())


def _anthropic() -> AIBackend:
    from ledger_agent.core.intelligence.ai_backend.anthropic_backend import AnthropicBackend
    return _chain(AnthropicBackend())


def _openai_compatible() -> AIBackend:
    from ledger_agent.core.intelligence.ai_backend.openai_backend import (
        OpenAICompatibleBackend,
    )
    return _chain(OpenAICompatibleBackend())


def _ollama() -> AIBackend:
    from ledger_agent.core.config import OLLAMA_DEFAULT_BASE_URL
    from ledger_agent.core.intelligence.ai_backend.openai_backend import (
        OpenAICompatibleBackend,
    )
    return _chain(OpenAICompatibleBackend(default_base_url=OLLAMA_DEFAULT_BASE_URL))


# name → factory.  Remote factories return ChainedBackend(local → remote).
PROVIDERS: dict[str, Callable[[], AIBackend]] = {
    "local": _local,
    "openai": _openai,
    "gemini": _gemini,
    "anthropic": _anthropic,
    "openai_compatible": _openai_compatible,
    "ollama": _ollama,
}

# Names that need validate_ai_config() before construction.
_REMOTE = frozenset(PROVIDERS) - {"local"}


def supported_backends() -> list[str]:
    """Return the sorted list of accepted FI_AI_BACKEND values."""
    return sorted(PROVIDERS)


def build_backend(name: str) -> AIBackend:
    """
    Construct the backend registered under ``name``.

    Unknown names log a WARNING and return LocalBackend.  Remote backends are
    validated (API key presence etc.) via config.validate_ai_config(); errors
    from validation or a missing SDK propagate so misconfiguration is visible.
    """
    key = (name or "local").strip().lower()
    factory = PROVIDERS.get(key)
    if factory is None:
        log.warning(
            "Unknown FI_AI_BACKEND value; using local backend",
            extra={"backend": key, "supported": ",".join(supported_backends())},
        )
        return _local()
    if key in _REMOTE:
        from ledger_agent.core.config import validate_ai_config
        validate_ai_config(key)
    return factory()


def get_backend(force_reload: bool = False) -> AIBackend:
    """
    Return (and cache) the configured AI backend singleton.

    Always runs local rules first.  Remote AI is only consulted when local
    confidence is below FI_LOCAL_CONFIDENCE_THRESHOLD (default 0.65).
    """
    global _instance
    if _instance is not None and not force_reload:
        return _instance

    from ledger_agent.core.config import AI_BACKEND

    _instance = build_backend(AI_BACKEND)
    return _instance


__all__ = ["AIBackend", "PROVIDERS", "build_backend", "get_backend", "supported_backends"]

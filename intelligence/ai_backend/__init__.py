"""
intelligence/ai_backend/__init__.py
────────────────────────────────────
Factory for AI backends.  Import get_backend() and use it everywhere so
the rest of the codebase stays backend-agnostic.

Backends:
  local   – rule-based + rapidfuzz (no API key required, default)
  openai  – OpenAI Chat Completions (requires FI_OPENAI_API_KEY)
  gemini  – Google Gemini (requires FI_GEMINI_API_KEY)
"""
from __future__ import annotations

from typing import Optional

from intelligence.ai_backend.base import AIBackend

_instance: Optional[AIBackend] = None


def get_backend(force_reload: bool = False) -> AIBackend:
    """
    Return (and cache) the configured AI backend singleton.

    Environment variable FI_AI_BACKEND selects the backend:
      local   → LocalBackend  (default, no API key needed)
      openai  → OpenAIBackend
      gemini  → GeminiBackend

    Raises ValueError if an AI backend is selected but key is missing.
    """
    global _instance
    if _instance is not None and not force_reload:
        return _instance

    from config import AI_BACKEND, validate_ai_config

    if AI_BACKEND in ("openai", "gemini"):
        validate_ai_config()

    if AI_BACKEND == "openai":
        from intelligence.ai_backend.openai_backend import OpenAIBackend
        _instance = OpenAIBackend()
    elif AI_BACKEND == "gemini":
        from intelligence.ai_backend.gemini_backend import GeminiBackend
        _instance = GeminiBackend()
    else:
        from intelligence.ai_backend.local_backend import LocalBackend
        _instance = LocalBackend()

    return _instance


__all__ = ["AIBackend", "get_backend"]

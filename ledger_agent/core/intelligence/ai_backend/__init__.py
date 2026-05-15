"""
intelligence/ai_backend/__init__.py  –  AI backend factory

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
  local   → LocalBackend only          (default, no API key, works offline)
  openai  → ChainedBackend(local→GPT)  (OpenAI called only for low-confidence)
  gemini  → ChainedBackend(local→Gem)  (Gemini called only for low-confidence)

Cost model:
  - local:  zero cost / transaction, no network, instantaneous
  - openai: sub-cent cost per uncertain transaction (low volume for small business)
  - gemini: sub-cent cost per uncertain transaction (typically cheaper than openai)
"""
from __future__ import annotations

from typing import Optional

from ledger_agent.core.intelligence.ai_backend.base import AIBackend

_instance: Optional[AIBackend] = None


def get_backend(force_reload: bool = False) -> AIBackend:
    """
    Return (and cache) the configured AI backend singleton.

    Always runs local rules first.  Remote AI is only consulted when local
    confidence is below FI_LOCAL_CONFIDENCE_THRESHOLD (default 0.65).
    """
    global _instance
    if _instance is not None and not force_reload:
        return _instance

    from config import AI_BACKEND, validate_ai_config

    if AI_BACKEND == "openai":
        validate_ai_config()
        from ledger_agent.core.intelligence.ai_backend.openai_backend import OpenAIBackend
        from ledger_agent.core.intelligence.ai_backend.chained_backend import ChainedBackend
        _instance = ChainedBackend(remote=OpenAIBackend())

    elif AI_BACKEND == "gemini":
        validate_ai_config()
        from ledger_agent.core.intelligence.ai_backend.gemini_backend import GeminiBackend
        from ledger_agent.core.intelligence.ai_backend.chained_backend import ChainedBackend
        _instance = ChainedBackend(remote=GeminiBackend())

    else:
        from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
        _instance = LocalBackend()

    return _instance


__all__ = ["AIBackend", "get_backend"]

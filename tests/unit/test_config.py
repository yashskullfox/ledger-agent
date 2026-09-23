"""
tests/unit/test_config.py  –  Unit tests for configuration validation
"""
from __future__ import annotations

import pytest


def test_validate_ai_config_openai_missing_key(monkeypatch):
    import ledger_agent.core.config as config_mod

    monkeypatch.setattr(config_mod, "AI_BACKEND", "openai")
    monkeypatch.setattr(config_mod, "ai_api_key", lambda: None)

    with pytest.raises(ValueError, match="FI_OPENAI_API_KEY"):
        config_mod.validate_ai_config()


def test_validate_ai_config_openai_present_key(monkeypatch):
    import ledger_agent.core.config as config_mod

    monkeypatch.setattr(config_mod, "AI_BACKEND", "openai")
    monkeypatch.setattr(config_mod, "ai_api_key", lambda: "test-openai-key")

    # Should not raise
    config_mod.validate_ai_config()


def test_validate_ai_config_gemini_missing_key(monkeypatch):
    import ledger_agent.core.config as config_mod

    monkeypatch.setattr(config_mod, "AI_BACKEND", "gemini")
    monkeypatch.setattr(config_mod, "ai_api_key", lambda: None)

    with pytest.raises(ValueError, match="FI_GEMINI_API_KEY"):
        config_mod.validate_ai_config()


def test_validate_ai_config_gemini_present_key(monkeypatch):
    import ledger_agent.core.config as config_mod

    monkeypatch.setattr(config_mod, "AI_BACKEND", "gemini")
    monkeypatch.setattr(config_mod, "ai_api_key", lambda: "test-gemini-key")

    # Should not raise
    config_mod.validate_ai_config()


def test_validate_ai_config_local_backend(monkeypatch):
    import ledger_agent.core.config as config_mod

    monkeypatch.setattr(config_mod, "AI_BACKEND", "local")
    monkeypatch.setattr(config_mod, "ai_api_key", lambda: None)

    # Should not raise
    config_mod.validate_ai_config()

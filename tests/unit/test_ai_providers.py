"""
tests/unit/test_ai_providers.py  –  Multi-provider AI backend registry + shared pipeline

No network: every provider SDK is replaced with an in-process fake injected
through sys.modules, so these tests pass whether or not openai / anthropic /
google-generativeai are installed.
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import privacy
from ledger_agent.core.intelligence import ai_backend
from ledger_agent.core.intelligence.ai_backend import _remote
from ledger_agent.core.intelligence.ai_backend._remote import (
    CLASSIFY_SYSTEM_PROMPT,
    extract_json,
    validate_classification,
)
from ledger_agent.core.intelligence.ai_backend.chained_backend import ChainedBackend
from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend

ACCOUNT_DIGITS = "123456789012"  # redaction: allow (synthetic test value)
SENSITIVE_DESC = f"ACCOUNT {ACCOUNT_DIGITS} PAYMENT ZZQX"
GOOD_JSON = json.dumps({
    "coa_code": "5010",
    "coa_name": "Software & Subscriptions",
    "is_transfer": False,
    "confidence": 0.9,
    "reason": "subscription",
})


# ── Fake SDKs ─────────────────────────────────────────────────────────────────

class _FakeAnthropicClient:
    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.reply = GOOD_JSON
        self.error: Exception | None = None
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(
            stop_reason="end_turn",
            content=[
                types.SimpleNamespace(type="thinking", thinking=""),
                types.SimpleNamespace(type="text", text=self.reply),
            ],
        )


class _FakeOpenAIClient:
    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.reply = GOOD_JSON
        self.error: Exception | None = None
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        msg = types.SimpleNamespace(content=self.reply)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


@pytest.fixture
def fake_sdks(monkeypatch):
    """Install fake anthropic / openai / google.generativeai modules."""
    anthropic_mod = types.ModuleType("anthropic")
    anthropic_mod.Anthropic = _FakeAnthropicClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", anthropic_mod)

    openai_mod = types.ModuleType("openai")
    openai_mod.OpenAI = _FakeOpenAIClient  # type: ignore[attr-defined]
    openai_mod.RateLimitError = type("RateLimitError", (Exception,), {})  # type: ignore[attr-defined]
    openai_mod.APITimeoutError = type("APITimeoutError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", openai_mod)

    genai_mod = types.ModuleType("google.generativeai")
    genai_mod.configure = MagicMock()  # type: ignore[attr-defined]
    genai_mod.GenerativeModel = MagicMock()  # type: ignore[attr-defined]
    google_mod = types.ModuleType("google")
    google_mod.generativeai = genai_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.generativeai", genai_mod)
    return types.SimpleNamespace(anthropic=anthropic_mod, openai=openai_mod, genai=genai_mod)


@pytest.fixture(autouse=True)
def _egress_redact(monkeypatch):
    """Force the default redact egress mode (other tests may leave 'mock' set)."""
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "OPENAI_BASE_URL", "")


@pytest.fixture
def audit_spy(monkeypatch):
    """Record every audit_egress() payload while still running the real check."""
    seen: list[Any] = []
    real = privacy.audit_egress

    def _spy(payload: Any) -> None:
        seen.append(payload)
        real(payload)

    monkeypatch.setattr(privacy, "audit_egress", _spy)
    return seen


def _anthropic_backend(monkeypatch):
    monkeypatch.setenv("FI_ANTHROPIC_API_KEY", "test-key-not-real")
    from ledger_agent.core.intelligence.ai_backend.anthropic_backend import AnthropicBackend
    return AnthropicBackend()


def _compat_backend(monkeypatch, default_base_url: str | None = None):
    monkeypatch.delenv("FI_OPENAI_API_KEY", raising=False)
    from ledger_agent.core.intelligence.ai_backend.openai_backend import (
        OpenAICompatibleBackend,
    )
    return OpenAICompatibleBackend(default_base_url=default_base_url)


# ── Registry ──────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_supported_names(self):
        assert set(ai_backend.supported_backends()) == {
            "local", "openai", "gemini", "anthropic", "openai_compatible", "ollama",
        }

    def test_local(self):
        assert isinstance(ai_backend.build_backend("local"), LocalBackend)

    @pytest.mark.parametrize("name, cls_name, backend_name", [
        ("openai", "OpenAIBackend", "openai"),
        ("gemini", "GeminiBackend", "gemini"),
        ("anthropic", "AnthropicBackend", "anthropic"),
        ("openai_compatible", "OpenAICompatibleBackend", "openai_compatible"),
        ("ollama", "OpenAICompatibleBackend", "openai_compatible"),
        ("  Anthropic ", "AnthropicBackend", "anthropic"),
    ])
    def test_remote_names_build_chained(self, monkeypatch, fake_sdks, name, cls_name,
                                        backend_name):
        monkeypatch.setenv("FI_OPENAI_API_KEY", "test-key-not-real")
        monkeypatch.setenv("FI_GEMINI_API_KEY", "test-key-not-real")
        monkeypatch.setenv("FI_ANTHROPIC_API_KEY", "test-key-not-real")
        backend = ai_backend.build_backend(name)
        assert isinstance(backend, ChainedBackend)
        assert type(backend._remote).__name__ == cls_name
        assert backend._remote.backend_name == backend_name

    def test_unknown_name_warns_and_uses_local(self, monkeypatch):
        fake_log = MagicMock()
        monkeypatch.setattr(ai_backend, "log", fake_log)
        backend = ai_backend.build_backend("no-such-provider")
        assert isinstance(backend, LocalBackend)
        fake_log.warning.assert_called_once()

    def test_get_backend_unknown_value_never_crashes(self, monkeypatch):
        monkeypatch.setattr(_cfg, "AI_BACKEND", "bogus")
        monkeypatch.setattr(ai_backend, "log", MagicMock())
        try:
            assert isinstance(ai_backend.get_backend(force_reload=True), LocalBackend)
        finally:
            ai_backend._instance = None

    def test_anthropic_requires_key(self, monkeypatch, fake_sdks):
        monkeypatch.delenv("FI_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="FI_ANTHROPIC_API_KEY"):
            ai_backend.build_backend("anthropic")

    def test_anthropic_accepts_unprefixed_key(self, monkeypatch, fake_sdks):
        monkeypatch.delenv("FI_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
        backend = ai_backend.build_backend("anthropic")
        assert backend._remote._client.init_kwargs["api_key"] == "test-key-not-real"

    @pytest.mark.parametrize("name", ["openai_compatible", "ollama"])
    def test_openai_compatible_needs_no_key(self, monkeypatch, fake_sdks, name):
        monkeypatch.delenv("FI_OPENAI_API_KEY", raising=False)
        _cfg.validate_ai_config(name)  # must not raise
        backend = ai_backend.build_backend(name)
        assert backend._remote._client.init_kwargs["api_key"] == "not-needed"


# ── JSON extraction / validation ──────────────────────────────────────────────

class TestJsonHelpers:
    def test_plain(self):
        assert extract_json(GOOD_JSON)["coa_code"] == "5010"

    def test_json_fence(self):
        assert extract_json(f"```json\n{GOOD_JSON}\n```")["coa_code"] == "5010"

    def test_bare_fence_with_prose(self):
        raw = f"Here is the answer:\n```\n{GOOD_JSON}\n```\nHope this helps."
        assert extract_json(raw)["coa_code"] == "5010"

    def test_prose_without_fence(self):
        raw = f"Sure! {GOOD_JSON} Let me know."
        assert extract_json(raw)["coa_code"] == "5010"

    def test_nested_braces(self):
        raw = 'x {"coa_code": "5010", "meta": {"a": {"b": 1}}} y'
        assert extract_json(raw)["meta"] == {"a": {"b": 1}}

    @pytest.mark.parametrize("raw", ["", "no json here", "{broken", "[1, 2]"])
    def test_no_object_raises(self, raw):
        with pytest.raises(ValueError):
            extract_json(raw)

    @pytest.mark.parametrize("code", ["1234", "ABCD", "", None, True, ["5010"]])
    def test_invalid_code_rejected(self, code):
        with pytest.raises(ValueError):
            validate_classification({"coa_code": code, "confidence": 0.9}, 0.8)

    def test_int_code_coerced_and_name_filled(self):
        out = validate_classification({"coa_code": 5010}, 0.8)
        assert out["coa_code"] == "5010"
        assert out["coa_name"] == "Software & Subscriptions"
        assert out["confidence"] == 0.8

    @pytest.mark.parametrize("given, expected", [(1.7, 1.0), (-0.3, 0.0), ("0.42", 0.42)])
    def test_confidence_clamped(self, given, expected):
        out = validate_classification({"coa_code": "5010", "confidence": given}, 0.8)
        assert out["confidence"] == pytest.approx(expected)

    def test_non_numeric_confidence_rejected(self):
        with pytest.raises(ValueError):
            validate_classification({"coa_code": "5010", "confidence": "high"}, 0.8)

    def test_context_coa_entries_extend_known_codes(self):
        entry = types.SimpleNamespace(code="6123", name="Custom Bucket")
        out = validate_classification({"coa_code": "6123"}, 0.8, {"coa_entries": [entry]})
        assert out["coa_name"] == "Custom Bucket"


# ── Prompt contract ───────────────────────────────────────────────────────────

class TestPrompt:
    def test_prompt_is_language_agnostic(self):
        lowered = CLASSIFY_SYSTEM_PROMPT.lower()
        assert "any language" in lowered
        assert "json" in lowered
        assert "5999" in CLASSIFY_SYSTEM_PROMPT

    def test_user_message_has_no_entity_line(self):
        msg = _remote.build_classify_user_message("SAFE DESC", -12.5)
        assert "Entity" not in msg
        assert "debit" in msg


# ── Anthropic backend ─────────────────────────────────────────────────────────

class TestAnthropicBackend:
    def test_redacts_and_audits(self, monkeypatch, fake_sdks, audit_spy):
        backend = _anthropic_backend(monkeypatch)
        result = backend.classify_transaction(SENSITIVE_DESC, -42.0)

        assert result["coa_code"] == "5010"
        assert len(backend._client.calls) == 1
        sent = json.dumps(backend._client.calls[0], default=str)
        assert ACCOUNT_DIGITS not in sent
        assert audit_spy, "audit_egress was not called"
        assert ACCOUNT_DIGITS not in json.dumps(audit_spy, default=str)

    def test_request_shape(self, monkeypatch, fake_sdks):
        backend = _anthropic_backend(monkeypatch)
        backend.classify_transaction("SOME SAAS VENDOR", -10.0)
        call = backend._client.calls[0]
        assert call["model"] == _cfg.ANTHROPIC_MODEL
        assert call["system"] == CLASSIFY_SYSTEM_PROMPT
        assert call["messages"][0]["role"] == "user"
        assert call["max_tokens"] <= 2048
        assert "temperature" not in call

    def test_client_exception_falls_back_to_local(self, monkeypatch, fake_sdks):
        backend = _anthropic_backend(monkeypatch)
        backend._client.error = RuntimeError("boom")
        desc = "ZZQX UNKNOWN MERCHANT"
        assert backend.classify_transaction(desc, -5.0) == \
            LocalBackend().classify_transaction(desc, -5.0)

    def test_invalid_code_falls_back_to_local(self, monkeypatch, fake_sdks):
        backend = _anthropic_backend(monkeypatch)
        backend._client.reply = '{"coa_code": "7777", "confidence": 0.99}'
        desc = "ZZQX UNKNOWN MERCHANT"
        assert backend.classify_transaction(desc, -5.0) == \
            LocalBackend().classify_transaction(desc, -5.0)

    def test_fenced_reply_parsed(self, monkeypatch, fake_sdks):
        backend = _anthropic_backend(monkeypatch)
        backend._client.reply = f"```json\n{GOOD_JSON}\n```"
        assert backend.classify_transaction("VENDOR", -1.0)["coa_code"] == "5010"

    def test_refusal_falls_back(self, monkeypatch, fake_sdks):
        backend = _anthropic_backend(monkeypatch)
        backend._client.messages.create = lambda **kw: types.SimpleNamespace(
            stop_reason="refusal", content=[])
        desc = "ZZQX UNKNOWN MERCHANT"
        assert backend.classify_transaction(desc, -5.0) == \
            LocalBackend().classify_transaction(desc, -5.0)

    def test_audit_failure_blocks_send(self, monkeypatch, fake_sdks):
        backend = _anthropic_backend(monkeypatch)

        def _block(_payload):
            raise privacy.PrivacyLeakError("blocked")

        monkeypatch.setattr(privacy, "audit_egress", _block)
        backend.classify_transaction("VENDOR", -1.0)
        assert backend._client.calls == []

    def test_mock_mode_makes_no_call(self, monkeypatch, fake_sdks):
        backend = _anthropic_backend(monkeypatch)
        monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "mock")
        result = backend.classify_transaction("VENDOR", -1.0)
        assert result["source"] == "mock"
        assert backend._client.calls == []

    def test_explain_and_enhance_redacted(self, monkeypatch, fake_sdks, audit_spy):
        backend = _anthropic_backend(monkeypatch)
        backend._client.reply = "Because it is a subscription."
        text = backend.explain_classification(SENSITIVE_DESC, "5010", "Software")
        assert "subscription" in text
        backend._client.reply = '{"enhanced_pattern": "X", "suggested_aliases": []}'
        assert backend.enhance_memory_rule(SENSITIVE_DESC, "5010", 3)["enhanced_pattern"] == "X"
        assert ACCOUNT_DIGITS not in json.dumps(backend._client.calls, default=str)
        assert len(audit_spy) == 2


# ── OpenAI-compatible / Ollama backend ────────────────────────────────────────

class TestOpenAICompatibleBackend:
    def test_ollama_default_base_url_and_placeholder_key(self, monkeypatch, fake_sdks):
        backend = _compat_backend(monkeypatch, default_base_url=_cfg.OLLAMA_DEFAULT_BASE_URL)
        assert backend._client.init_kwargs["base_url"] == "http://localhost:11434/v1"
        assert backend._client.init_kwargs["api_key"] == "not-needed"

    def test_base_url_env_wins(self, monkeypatch, fake_sdks):
        monkeypatch.setattr(_cfg, "OPENAI_BASE_URL", "http://gpu-box:8000/v1")
        backend = _compat_backend(monkeypatch, default_base_url=_cfg.OLLAMA_DEFAULT_BASE_URL)
        assert backend._client.init_kwargs["base_url"] == "http://gpu-box:8000/v1"

    def test_redacts_and_audits_even_for_localhost(self, monkeypatch, fake_sdks, audit_spy):
        backend = _compat_backend(monkeypatch, default_base_url=_cfg.OLLAMA_DEFAULT_BASE_URL)
        result = backend.classify_transaction(SENSITIVE_DESC, -42.0)
        assert result["coa_code"] == "5010"
        sent = json.dumps(backend._client.calls[0], default=str)
        assert ACCOUNT_DIGITS not in sent
        assert audit_spy, "audit_egress was not called"
        assert backend._client.calls[0]["max_tokens"] == 256

    def test_client_exception_falls_back_to_local(self, monkeypatch, fake_sdks):
        backend = _compat_backend(monkeypatch)
        backend._client.error = ConnectionError("endpoint down")
        desc = "ZZQX UNKNOWN MERCHANT"
        assert backend.classify_transaction(desc, -5.0) == \
            LocalBackend().classify_transaction(desc, -5.0)


# ── OpenAI backend ────────────────────────────────────────────────────────────

class TestOpenAIBackend:
    def _backend(self, monkeypatch):
        monkeypatch.setenv("FI_OPENAI_API_KEY", "test-key-not-real")
        monkeypatch.setattr(_cfg, "AI_BACKEND", "openai")
        from ledger_agent.core.intelligence.ai_backend.openai_backend import OpenAIBackend
        return OpenAIBackend()

    def test_base_url_passed_when_set(self, monkeypatch, fake_sdks):
        monkeypatch.setattr(_cfg, "OPENAI_BASE_URL", "https://gateway.example/v1")
        backend = self._backend(monkeypatch)
        assert backend._client.init_kwargs["base_url"] == "https://gateway.example/v1"

    def test_no_base_url_by_default(self, monkeypatch, fake_sdks):
        backend = self._backend(monkeypatch)
        assert "base_url" not in backend._client.init_kwargs

    def test_uses_max_completion_tokens(self, monkeypatch, fake_sdks, audit_spy):
        backend = self._backend(monkeypatch)
        backend.classify_transaction(SENSITIVE_DESC, -3.0)
        call = backend._client.calls[0]
        assert call["max_completion_tokens"] == 256
        assert ACCOUNT_DIGITS not in json.dumps(call, default=str)
        assert audit_spy

    def test_old_sdk_typeerror_falls_back_to_max_tokens(self, monkeypatch, fake_sdks):
        backend = self._backend(monkeypatch)
        calls: list[dict] = []

        def _create(**kwargs):
            calls.append(kwargs)
            if "max_completion_tokens" in kwargs:
                raise TypeError("unexpected keyword argument 'max_completion_tokens'")
            msg = types.SimpleNamespace(content=GOOD_JSON)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        backend._client.chat.completions.create = _create
        assert backend.classify_transaction("VENDOR", -3.0)["coa_code"] == "5010"
        assert calls[-1]["max_tokens"] == 256


# ── Multilingual ──────────────────────────────────────────────────────────────

MULTILINGUAL = [
    "東京電力 電気料金 2月分",                 # Japanese: utility bill
    "Überweisung Büromaterial GmbH Rechnung",  # German: office supplies invoice
    "Pago de suscripción de software",         # Spanish
    "Абонентская плата за ПО",                 # Russian
]


class TestMultilingual:
    @pytest.mark.parametrize("desc", MULTILINGUAL)
    def test_non_ascii_through_remote_pipeline(self, monkeypatch, fake_sdks, audit_spy, desc):
        backend = _anthropic_backend(monkeypatch)
        result = backend.classify_transaction(desc, -19.99)
        assert result["coa_code"] == "5010"
        assert audit_spy
        # Description is sent as-is (redacted only), never translated or mangled.
        assert desc in backend._client.calls[0]["messages"][0]["content"]

    @pytest.mark.parametrize("desc", MULTILINGUAL)
    def test_non_ascii_with_account_number_redacted(self, monkeypatch, fake_sdks, desc):
        backend = _compat_backend(monkeypatch)
        backend.classify_transaction(f"{desc} ACCOUNT {ACCOUNT_DIGITS}", -5.0)
        assert ACCOUNT_DIGITS not in json.dumps(backend._client.calls, ensure_ascii=False)

    def test_bare_digit_run_redacted(self, monkeypatch, fake_sdks):
        backend = _compat_backend(monkeypatch)
        backend.classify_transaction(f"Pago de software {ACCOUNT_DIGITS}", -5.0)
        assert ACCOUNT_DIGITS not in json.dumps(backend._client.calls, ensure_ascii=False)

    @pytest.mark.parametrize("desc", MULTILINGUAL)
    def test_non_ascii_local_and_chained(self, monkeypatch, fake_sdks, desc):
        result = LocalBackend().classify_transaction(desc, -1.0)
        assert isinstance(result["coa_code"], str)
        chained = ChainedBackend(remote=_anthropic_backend(monkeypatch), threshold=0.99)
        assert chained.classify_transaction(desc, -1.0)["coa_code"]


# ── X-07: CodeQL quality fixes ────────────────────────────────────────────────

NON_FINITE_CONFIDENCE = ["nan", "NaN", float("nan"), "inf", "-inf", float("inf"), float("-inf")]


class TestNonFiniteConfidence:
    @pytest.mark.parametrize("given", NON_FINITE_CONFIDENCE)
    def test_validate_rejects_nan_and_inf(self, given):
        with pytest.raises(ValueError, match="confidence must be numeric"):
            validate_classification({"coa_code": "5010", "confidence": given}, 0.8)

    @pytest.mark.parametrize("reply", [
        '{"coa_code": "5010", "confidence": "nan"}',
        '{"coa_code": "5010", "confidence": NaN}',
        '{"coa_code": "5010", "confidence": "inf"}',
        '{"coa_code": "5010", "confidence": "-inf"}',
        '{"coa_code": "5010", "confidence": Infinity}',
        '{"coa_code": "5010", "confidence": -Infinity}',
    ])
    def test_non_finite_confidence_falls_back_to_local(self, monkeypatch, fake_sdks, reply):
        backend = _compat_backend(monkeypatch)
        backend._client.reply = reply
        desc = "ZZQX UNKNOWN MERCHANT"
        assert backend.classify_transaction(desc, -5.0) == \
            LocalBackend().classify_transaction(desc, -5.0)
        assert len(backend._client.calls) == 1


class TestOpenAIInitChain:
    def test_compatible_init_runs_parent_init(self, monkeypatch, fake_sdks):
        from ledger_agent.core.intelligence.ai_backend import openai_backend

        seen: list[str] = []
        real = openai_backend.OpenAIBackend.__init__

        def _spy(self) -> None:
            seen.append(type(self).__name__)
            real(self)

        monkeypatch.setattr(openai_backend.OpenAIBackend, "__init__", _spy)
        _compat_backend(monkeypatch)
        assert seen == ["OpenAICompatibleBackend"]

    def test_compatible_client_kwargs_unchanged(self, monkeypatch, fake_sdks):
        backend = _compat_backend(monkeypatch)
        assert backend._client.init_kwargs == {
            "api_key": "not-needed",
            "base_url": _cfg.OLLAMA_DEFAULT_BASE_URL,
            "timeout": 60.0,
        }
        assert backend._model == _cfg.OPENAI_MODEL
        assert backend._scope == "openai_compatible"
        assert backend._token_param == "max_tokens"
        assert backend.backend_name == "openai_compatible"

    def test_compatible_explicit_default_base_url(self, monkeypatch, fake_sdks):
        backend = _compat_backend(monkeypatch, default_base_url="http://vllm.example:8000/v1")
        assert backend._client.init_kwargs["base_url"] == "http://vllm.example:8000/v1"

    def test_compatible_uses_its_own_key(self, monkeypatch, fake_sdks):
        monkeypatch.setenv("FI_OPENAI_API_KEY", "compat-key-not-real")
        from ledger_agent.core.intelligence.ai_backend.openai_backend import (
            OpenAICompatibleBackend,
        )
        backend = OpenAICompatibleBackend()
        assert backend._client.init_kwargs["api_key"] == "compat-key-not-real"

    def test_openai_client_kwargs_unchanged(self, monkeypatch, fake_sdks):
        monkeypatch.setenv("FI_OPENAI_API_KEY", "test-key-not-real")
        monkeypatch.setattr(_cfg, "AI_BACKEND", "openai")
        from ledger_agent.core.intelligence.ai_backend.openai_backend import OpenAIBackend
        backend = OpenAIBackend()
        # api_key comes from config.ai_api_key() exactly as before (resolved per FI_AI_BACKEND).
        assert backend._client.init_kwargs == {"api_key": _cfg.ai_api_key(), "timeout": 30.0}
        assert backend._model == _cfg.OPENAI_MODEL
        assert backend._scope == "openai"
        assert backend._token_param == "max_completion_tokens"

    def test_missing_sdk_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "openai", None)
        from ledger_agent.core.intelligence.ai_backend.openai_backend import (
            OpenAICompatibleBackend,
        )
        with pytest.raises(ImportError):
            OpenAICompatibleBackend()

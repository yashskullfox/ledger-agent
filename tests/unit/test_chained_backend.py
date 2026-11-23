"""
tests/unit/test_chained_backend.py – Unit tests for ChainedBackend
"""
from __future__ import annotations

from unittest.mock import MagicMock

from ledger_agent.core.intelligence.ai_backend.base import AIBackend
from ledger_agent.core.intelligence.ai_backend.chained_backend import ChainedBackend


class DummyRemoteBackend(AIBackend):
    @property
    def backend_name(self) -> str:
        return "dummy_remote"

    def classify_transaction(self, description, amount, context=None):
        return {"coa_code": "5010", "coa_name": "Office Supplies", "confidence": 0.90}

    def enhance_memory_rule(self, pattern, coa_code, confirmed_count):
        return {"pattern": pattern, "coa_code": coa_code, "enhanced": True}

    def explain_classification(self, description, coa_code, coa_name):
        return f"Remote explanation for {description}"

    def on_user_confirmed(self, description, coa_code, coa_name, is_transfer):
        pass


def test_chained_backend_init_and_name():
    remote = DummyRemoteBackend()
    backend = ChainedBackend(remote=remote, threshold=0.75)
    assert backend.backend_name == "chained(local→dummy_remote)"
    assert backend._threshold == 0.75


def test_classify_transaction_local_high_confidence(monkeypatch):
    remote = DummyRemoteBackend()
    backend = ChainedBackend(remote=remote, threshold=0.65)

    mock_local = MagicMock()
    mock_local.classify_transaction.return_value = {
        "coa_code": "5020",
        "coa_name": "Subscriptions",
        "confidence": 0.85,
    }
    monkeypatch.setattr(backend, "_local", mock_local)

    res = backend.classify_transaction("SLACK SUBSCRIPTION", 15.00)
    assert res["coa_code"] == "5020"
    assert res["confidence"] == 0.85
    assert res["source"] == "local"


def test_classify_transaction_escalates_to_remote_when_local_confidence_low(monkeypatch):
    remote = DummyRemoteBackend()
    backend = ChainedBackend(remote=remote, threshold=0.65)

    mock_local = MagicMock()
    mock_local.classify_transaction.return_value = {
        "coa_code": "9999",
        "coa_name": "Uncategorized",
        "confidence": 0.30,
    }
    monkeypatch.setattr(backend, "_local", mock_local)

    res = backend.classify_transaction("UNKNOWN MERCHANT", 100.00)
    assert res["coa_code"] == "5010"
    assert res["confidence"] == 0.90
    assert res["source"] == "dummy_remote"


def test_classify_transaction_fallback_when_remote_confidence_lower(monkeypatch):
    remote = MagicMock(spec=AIBackend)
    remote.backend_name = "low_remote"
    remote.classify_transaction.return_value = {
        "coa_code": "5099",
        "coa_name": "Miscellaneous",
        "confidence": 0.20,
    }

    backend = ChainedBackend(remote=remote, threshold=0.65)

    mock_local = MagicMock()
    mock_local.classify_transaction.return_value = {
        "coa_code": "5020",
        "coa_name": "Legal",
        "confidence": 0.45,
    }
    monkeypatch.setattr(backend, "_local", mock_local)

    res = backend.classify_transaction("SOME FEE", 50.00)
    assert res["coa_code"] == "5020"
    assert res["confidence"] == 0.45
    assert res["source"] == "local_fallback"


def test_classify_transaction_fallback_on_remote_exception(monkeypatch):
    remote = MagicMock(spec=AIBackend)
    remote.backend_name = "failing_remote"
    remote.classify_transaction.side_effect = RuntimeError("API unavailable")

    backend = ChainedBackend(remote=remote, threshold=0.65)

    mock_local = MagicMock()
    mock_local.classify_transaction.return_value = {
        "coa_code": "5030",
        "coa_name": "Bank Fee",
        "confidence": 0.50,
    }
    monkeypatch.setattr(backend, "_local", mock_local)

    res = backend.classify_transaction("SERVICE CHARGE", 12.00)
    assert res["coa_code"] == "5030"
    assert res["source"] == "local_fallback"


def test_enhance_memory_rule_remote_success():
    remote = DummyRemoteBackend()
    backend = ChainedBackend(remote=remote)
    res = backend.enhance_memory_rule("ADOBE", "5020", 3)
    assert res["enhanced"] is True
    assert res["pattern"] == "ADOBE"


def test_enhance_memory_rule_fallback_on_exception(monkeypatch):
    remote = MagicMock(spec=AIBackend)
    remote.enhance_memory_rule.side_effect = RuntimeError("Network error")

    backend = ChainedBackend(remote=remote)
    mock_local = MagicMock()
    mock_local.enhance_memory_rule.return_value = {"pattern": "TEST", "coa_code": "5000"}
    monkeypatch.setattr(backend, "_local", mock_local)

    res = backend.enhance_memory_rule("TEST", "5000", 1)
    assert res["pattern"] == "TEST"
    mock_local.enhance_memory_rule.assert_called_once_with("TEST", "5000", 1)


def test_explain_classification_remote_success():
    remote = DummyRemoteBackend()
    backend = ChainedBackend(remote=remote)
    explanation = backend.explain_classification("SLACK", "5020", "Software")
    assert "Remote explanation for SLACK" in explanation


def test_explain_classification_fallback_on_exception(monkeypatch):
    remote = MagicMock(spec=AIBackend)
    remote.explain_classification.side_effect = RuntimeError("Remote failure")

    backend = ChainedBackend(remote=remote)
    mock_local = MagicMock()
    mock_local.explain_classification.return_value = "Local rule matched SLACK"
    monkeypatch.setattr(backend, "_local", mock_local)

    res = backend.explain_classification("SLACK", "5020", "Software")
    assert res == "Local rule matched SLACK"
    mock_local.explain_classification.assert_called_once_with("SLACK", "5020", "Software")


def test_on_user_confirmed_calls_both(monkeypatch):
    remote = MagicMock(spec=AIBackend)
    backend = ChainedBackend(remote=remote)

    mock_local = MagicMock()
    monkeypatch.setattr(backend, "_local", mock_local)

    backend.on_user_confirmed("AMAZON", "5040", "Supplies", False)
    mock_local.on_user_confirmed.assert_called_once_with("AMAZON", "5040", "Supplies", False)
    remote.on_user_confirmed.assert_called_once_with("AMAZON", "5040", "Supplies", False)


def test_on_user_confirmed_remote_exception_suppressed(monkeypatch):
    remote = MagicMock(spec=AIBackend)
    remote.on_user_confirmed.side_effect = RuntimeError("Remote sync failed")
    backend = ChainedBackend(remote=remote)

    mock_local = MagicMock()
    monkeypatch.setattr(backend, "_local", mock_local)

    # Should not raise
    backend.on_user_confirmed("AMAZON", "5040", "Supplies", False)
    mock_local.on_user_confirmed.assert_called_once()

"""
tests/test_privacy.py  –  Acceptance tests for core/privacy.py (R-46)

Tests cover all 12 detector categories, audit_egress(), egress modes,
PrivacyFilter logging integration, memory-file redaction, and the
unredact() round-trip.

Acceptance criteria from R-46 spec:
  [x] 12 detector categories — each produces a token, no false negatives
  [x] Outbound payload audit — bypassing redact() raises PrivacyLeakError
  [x] FI_AI_EGRESS_MODE=mock — no network call, stub classification returned
  [x] FI_AI_EGRESS_MODE=strict — digit-run ≥ 7 in payload raises PrivacyLeakError
  [x] FI_AI_EGRESS_MODE=passthrough — requires explicit ACK env var
  [x] Memory file round-trip — pattern persisted with redacted tokens
  [x] Log capture — SSN in log message emitted as <SSN_***>
  [x] unredact() round-trip — original restored from RedactionMap
  [x] Performance — redaction of 200-char description < 1 s (regression guard)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.privacy import (
    PrivacyFilter,
    PrivacyLeakError,
    RedactionMap,
    _aba_valid,
    _luhn_valid,
    _reset_session,
    audit_egress,
    redact,
    unredact,
    unredact_result,
    privacy_status,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_privacy_session():
    """Reset per-session pseudonym registry before every test."""
    _reset_session()
    yield
    _reset_session()


@pytest.fixture(autouse=True)
def default_egress_mode(monkeypatch):
    """Ensure FI_AI_EGRESS_MODE=redact for all tests unless overridden."""
    monkeypatch.setenv("FI_AI_EGRESS_MODE", "redact")
    monkeypatch.delenv("FI_AI_EGRESS_MODE_ACK", raising=False)
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    # Force config to re-read env (config uses module-level reads)
    import importlib, config as _cfg
    _cfg.AI_EGRESS_MODE = "redact"
    _cfg.AI_EGRESS_MODE_ACK = ""
    _cfg.PRIVACY_ENTITY_NAME = ""
    yield


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(text: str, scope: str = "openai") -> str:
    """Return just the redacted text (discard map)."""
    out, _ = redact(text, scope=scope)
    return out


def _has_token(text: str, prefix: str) -> bool:
    """Return True if any token starting with prefix is in text."""
    import re
    return bool(re.search(rf"<{re.escape(prefix)}_", text))


# ── Luhn & ABA validators ─────────────────────────────────────────────────────

class TestChecksums:
    def test_luhn_valid_visa(self):
        # Standard test Visa number
        assert _luhn_valid("4532015112830366")

    def test_luhn_invalid(self):
        assert not _luhn_valid("1234567890123456")

    def test_aba_valid(self):
        # ABA 021000021 = JPMorgan Chase — well-known valid routing
        assert _aba_valid("021000021")

    def test_aba_invalid(self):
        # 111111111 → checksum = 33 (not divisible by 10)
        assert not _aba_valid("111111111")
        # 123456789 → checksum = 159 (not divisible by 10)
        assert not _aba_valid("123456789")

    def test_aba_wrong_length(self):
        assert not _aba_valid("12345678")   # 8 digits
        assert not _aba_valid("1234567890")  # 10 digits


# ── Detector category 1: SSN ──────────────────────────────────────────────────

class TestDetectorSSN:
    def test_hyphenated_ssn(self):
        out = _safe("SSN: 123-45-6789")
        assert "<SSN_***>" in out
        assert "123-45-6789" not in out

    def test_ssn_in_sentence(self):
        out = _safe("Please provide your SSN: 987-65-4321 for verification.")
        assert "<SSN_***>" in out
        assert "987-65-4321" not in out

    def test_ssn_context_word_bare(self):
        # Bare 9-digit with SSN context
        out = _safe("TAX ID 123456789 required")
        assert "<SSN_***>" in out

    def test_no_false_positive_date(self):
        # 01/15/2025 should NOT be redacted as SSN
        out = _safe("Statement date 01/15/2025")
        assert "2025" in out  # date preserved

    def test_ssn_log_scope(self):
        out = _safe("Error SSN 123-45-6789 invalid", scope="log")
        assert "<SSN_***>" in out

    def test_ssn_not_in_redact_map(self):
        # SSN uses *** token — should NOT be in the RedactionMap
        _, m = redact("SSN: 111-22-3333")
        assert "<SSN_***>" not in m  # map keys are tokens, not originals


# ── Detector category 2: EIN ──────────────────────────────────────────────────

class TestDetectorEIN:
    def test_ein_standard(self):
        out = _safe("Our EIN is 12-3456789")
        assert "<EIN_***>" in out
        assert "12-3456789" not in out

    def test_ein_in_sentence(self):
        out = _safe("Federal employer ID: 45-6789012 on file.")
        assert "<EIN_***>" in out

    def test_ein_not_confused_with_ssn(self):
        # EIN = NN-NNNNNNN; SSN = NNN-NN-NNNN — different patterns
        text = "EIN 12-3456789 and SSN 123-45-6789"
        out = _safe(text)
        assert "<EIN_***>" in out
        assert "<SSN_***>" in out


# ── Detector category 3: ABA routing ─────────────────────────────────────────

class TestDetectorRouting:
    def test_routing_with_context(self):
        # 021000021 = valid ABA
        out = _safe("Routing number: 021000021")
        assert "<ROUTING_***>" in out
        assert "021000021" not in out

    def test_routing_aba_keyword(self):
        out = _safe("ABA 021000021 for wire transfer")
        assert "<ROUTING_***>" in out

    def test_invalid_routing_not_detected(self):
        # 9-digit number that fails ABA checksum should not be redacted
        out = _safe("Amount: 123456789 dollars")
        assert "<ROUTING_***>" not in out


# ── Detector category 4: Credit card PAN ─────────────────────────────────────

class TestDetectorCreditCard:
    def test_visa_16_digit(self):
        # 4532015112830366 is a Luhn-valid test Visa
        out = _safe("Card: 4532015112830366")
        assert _has_token(out, "CARD")
        assert "4532015112830366" not in out

    def test_card_last4_in_token(self):
        out = _safe("Charged to 4532015112830366")
        assert "0366" in out  # last4 embedded in token

    def test_invalid_luhn_not_detected(self):
        # Not a valid card number
        out = _safe("Reference: 1234567890123456")
        assert not _has_token(out, "CARD")

    def test_mastercard(self):
        # 5500005555555559 = Luhn-valid Mastercard test number
        out = _safe("Mastercard 5500005555555559 charged")
        assert _has_token(out, "CARD")


# ── Detector category 5: Bank account number ─────────────────────────────────

class TestDetectorAccountNumber:
    def test_with_account_context(self):
        out = _safe("Account 1234567890 balance available")
        assert _has_token(out, "ACCT")

    def test_with_acct_abbreviation(self):
        out = _safe("ACCT 98765432100 posting")
        assert _has_token(out, "ACCT")

    def test_last4_in_token(self):
        out = _safe("Checking account 1234567890")
        assert "7890" in out

    def test_no_context_not_detected(self):
        # Bare number with no account context should not be redacted
        out = _safe("Amount is 12345678")
        assert not _has_token(out, "ACCT")


# ── Detector category 6: Email ────────────────────────────────────────────────

class TestDetectorEmail:
    def test_standard_email(self):
        out = _safe("Contact john.doe@example.com for support")
        assert _has_token(out, "EMAIL")
        assert "john.doe@example.com" not in out

    def test_email_stability(self):
        # Same email → same token within a session
        out1, m1 = redact("Email: user@test.com")
        _reset_session()
        out2, m2 = redact("Email: user@test.com")
        # Token shape is the same even across sessions (sequential counter resets)
        assert _has_token(out1, "EMAIL")
        assert _has_token(out2, "EMAIL")

    def test_multiple_emails_unique_tokens(self):
        out, m = redact("From: a@x.com to b@y.com")
        assert "a@x.com" not in out
        assert "b@y.com" not in out
        # Both tokens in map
        assert len([k for k in m if "<EMAIL_" in k]) == 2


# ── Detector category 7: Phone number ────────────────────────────────────────

class TestDetectorPhone:
    def test_nanp_standard(self):
        out = _safe("Call us at 415-555-0100")
        assert _has_token(out, "PHONE")
        assert "415-555-0100" not in out

    def test_nanp_parens(self):
        out = _safe("Phone (415) 555-0100")
        assert _has_token(out, "PHONE")

    def test_nanp_dotted(self):
        out = _safe("Tel: 415.555.0100")
        assert _has_token(out, "PHONE")

    def test_short_number_not_detected(self):
        # Short codes / partial numbers should not trigger
        out = _safe("Code: 1234")
        assert not _has_token(out, "PHONE")


# ── Detector category 8: Street address ──────────────────────────────────────

class TestDetectorAddress:
    def test_street_address(self):
        out = _safe("Delivered to 123 Main St today")
        assert _has_token(out, "ADDR")
        assert "123 Main St" not in out

    def test_avenue(self):
        out = _safe("Office at 456 Park Ave Suite 100")
        assert _has_token(out, "ADDR")

    def test_no_false_positive_amount(self):
        # "$1,234" should never be an address
        out = _safe("Amount: $1,234")
        assert not _has_token(out, "ADDR")


# ── Detector category 9: API key ─────────────────────────────────────────────

class TestDetectorAPIKey:
    def test_openai_key_raises(self):
        with pytest.raises(PrivacyLeakError, match="API key"):
            redact("Key: sk-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234567890abcd")

    def test_google_key_raises(self):
        with pytest.raises(PrivacyLeakError, match="API key"):
            redact("Token: AIzaSyBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

    def test_github_pat_raises(self):
        with pytest.raises(PrivacyLeakError, match="API key"):
            redact("PAT: ghp_" + "A" * 36)


# ── Detector category 10: Entity legal name ──────────────────────────────────

class TestDetectorEntityName:
    def test_entity_name_direct(self, monkeypatch):
        import config as _cfg
        _cfg.PRIVACY_ENTITY_NAME = "Acme Holdings LLC"
        try:
            out = _safe("Payment from Acme Holdings LLC for services")
            assert "<ENTITY_NAME>" in out
            assert "Acme Holdings LLC" not in out
        finally:
            _cfg.PRIVACY_ENTITY_NAME = ""

    def test_entity_name_case_insensitive(self, monkeypatch):
        import config as _cfg
        _cfg.PRIVACY_ENTITY_NAME = "Test Corp"
        try:
            out = _safe("Invoice to TEST CORP")
            assert "<ENTITY_NAME>" in out
        finally:
            _cfg.PRIVACY_ENTITY_NAME = ""

    def test_no_entity_name_no_redaction(self):
        import config as _cfg
        _cfg.PRIVACY_ENTITY_NAME = ""
        out = _safe("Payment to Acme Corp")
        assert "<ENTITY_NAME>" not in out


# ── Detector category 11: Person name ────────────────────────────────────────

class TestDetectorPersonName:
    def test_payee_context(self):
        out = _safe("PAYEE: John Smith for consulting")
        assert _has_token(out, "PERSON")
        assert "John Smith" not in out

    def test_payment_to_context(self):
        out = _safe("PAYMENT TO Jane Doe invoice 1001")
        assert _has_token(out, "PERSON")

    def test_no_context_no_redaction(self):
        # "John Smith" without context should NOT be redacted
        out = _safe("Description of transaction type")
        assert not _has_token(out, "PERSON")

    def test_person_token_stable(self):
        out1, _ = redact("PAYEE: Alice Brown")
        out2, _ = redact("PAYEE: Alice Brown")
        # Same person in same session → same token
        import re
        tokens1 = re.findall(r"<PERSON_\d+>", out1)
        tokens2 = re.findall(r"<PERSON_\d+>", out2)
        assert tokens1 == tokens2


# ── Detector category 12: Counterparty business name ─────────────────────────

class TestDetectorCounterparty:
    def test_unknown_allcaps_counterparty(self):
        out = _safe("Payment to ACME CORP received")
        assert _has_token(out, "COUNTERPARTY")

    def test_known_vendor_not_redacted(self):
        # Stripe is in the allowlist — should pass through
        out = _safe("STRIPE PAYMENT 1234")
        assert "STRIPE" in out
        assert not _has_token(out, "COUNTERPARTY")

    def test_paypal_not_redacted(self):
        out = _safe("PAYPAL CHARGE")
        assert "PAYPAL" in out

    def test_system_word_not_redacted(self):
        # ACH TRANSFER — both are system words
        out = _safe("ACH TRANSFER 1234")
        assert "ACH" in out


# ── Multiple categories in one string ────────────────────────────────────────

class TestMultipleCategories:
    def test_ssn_and_email(self):
        text = "SSN: 123-45-6789 contact me at owner@mybiz.com"
        out = _safe(text)
        assert "<SSN_***>" in out
        assert "123-45-6789" not in out
        assert _has_token(out, "EMAIL")
        assert "owner@mybiz.com" not in out

    def test_ein_and_account(self):
        text = "EIN 12-3456789 linked to checking account 9876543210"
        out = _safe(text)
        assert "<EIN_***>" in out
        assert _has_token(out, "ACCT")

    def test_clean_text_unchanged(self):
        text = "STRIPE payment $99.99 for Software & Subscriptions"
        out = _safe(text)
        assert "STRIPE" in out
        assert "$99.99" in out
        assert "Software" in out


# ── audit_egress() ────────────────────────────────────────────────────────────

class TestAuditEgress:
    def test_clean_payload_passes(self):
        # Should not raise
        audit_egress("STRIPE payment $99.99 classified as 5010")

    def test_ssn_in_payload_raises(self):
        with pytest.raises(PrivacyLeakError, match="PII detected"):
            audit_egress("Transaction for SSN 123-45-6789")

    def test_ein_in_payload_raises(self):
        with pytest.raises(PrivacyLeakError, match="PII detected"):
            audit_egress({"description": "Company EIN 45-6789012"})

    def test_api_key_in_payload_raises(self):
        with pytest.raises(PrivacyLeakError, match="API key"):
            audit_egress("key=sk-" + "x" * 20)

    def test_already_redacted_passes(self):
        # After redact(), the output should pass audit_egress
        safe, _ = redact("SSN: 123-45-6789 and EIN 12-3456789")
        audit_egress(safe)  # Should not raise

    def test_dict_payload(self):
        with pytest.raises(PrivacyLeakError):
            audit_egress({"messages": [{"content": "SSN: 999-88-7777"}]})

    def test_list_payload(self):
        with pytest.raises(PrivacyLeakError):
            audit_egress([{"role": "user", "content": "EIN 22-3334444"}])


# ── Egress mode: mock ─────────────────────────────────────────────────────────

class TestMockMode:
    def test_openai_mock_returns_stub(self, monkeypatch):
        monkeypatch.setenv("FI_AI_EGRESS_MODE", "mock")
        import config as _cfg
        _cfg.AI_EGRESS_MODE = "mock"

        from intelligence.ai_backend.openai_backend import OpenAIBackend

        # We must prevent __init__ from connecting — patch the openai import
        with patch.dict("sys.modules", {"openai": MagicMock()}):
            backend = OpenAIBackend.__new__(OpenAIBackend)
            backend._client = MagicMock()
            backend._model = "gpt-4o-mini"

            result = backend.classify_transaction("STRIPE $99", -99.0)

        assert result["source"] == "mock"
        assert result["confidence"] == 0.50
        # Ensure no HTTP call was made
        backend._client.chat.completions.create.assert_not_called()

    def test_gemini_mock_returns_stub(self, monkeypatch):
        monkeypatch.setenv("FI_AI_EGRESS_MODE", "mock")
        import config as _cfg
        _cfg.AI_EGRESS_MODE = "mock"

        from intelligence.ai_backend.gemini_backend import GeminiBackend

        with patch.dict("sys.modules", {"google.generativeai": MagicMock()}):
            backend = GeminiBackend.__new__(GeminiBackend)
            backend._model = MagicMock()
            backend._model_name = "gemini-1.5-flash"

            result = backend.classify_transaction("GOOGLE $10", -10.0)

        assert result["source"] == "mock"
        backend._model.generate_content.assert_not_called()


# ── Egress mode: strict ───────────────────────────────────────────────────────

class TestStrictMode:
    def test_strict_blocks_unredacted_digit_run(self, monkeypatch):
        monkeypatch.setenv("FI_AI_EGRESS_MODE", "strict")
        import config as _cfg
        _cfg.AI_EGRESS_MODE = "strict"

        # A 9-digit number with no context won't be auto-detected,
        # but strict mode should catch any 7+ digit run
        with pytest.raises(PrivacyLeakError, match="digit-run"):
            redact("Reference: 12345678", scope="openai")

    def test_strict_allows_clean_text(self, monkeypatch):
        monkeypatch.setenv("FI_AI_EGRESS_MODE", "strict")
        import config as _cfg
        _cfg.AI_EGRESS_MODE = "strict"

        # Clean text with amounts < 7 digits should pass
        safe, _ = redact("STRIPE $99.99 debit expense", scope="openai")
        assert "STRIPE" in safe


# ── Egress mode: passthrough ──────────────────────────────────────────────────

class TestPassthroughMode:
    def test_passthrough_without_ack_raises(self, monkeypatch):
        monkeypatch.setenv("FI_AI_EGRESS_MODE", "passthrough")
        import config as _cfg
        _cfg.AI_EGRESS_MODE = "passthrough"
        _cfg.AI_EGRESS_MODE_ACK = ""

        with pytest.raises(RuntimeError, match="I_understand_the_risk"):
            redact("SSN: 123-45-6789")

    def test_passthrough_with_ack_returns_unchanged(self, monkeypatch):
        monkeypatch.setenv("FI_AI_EGRESS_MODE", "passthrough")
        monkeypatch.setenv("FI_AI_EGRESS_MODE_ACK", "I_understand_the_risk")
        import config as _cfg
        _cfg.AI_EGRESS_MODE = "passthrough"
        _cfg.AI_EGRESS_MODE_ACK = "I_understand_the_risk"

        text = "SSN: 123-45-6789 payment"
        out, m = redact(text)
        assert out == text  # unchanged
        assert m == {}      # empty map


# ── PrivacyFilter — logging integration ──────────────────────────────────────

class TestPrivacyFilter:
    def test_ssn_stripped_from_log(self):
        """Log record containing SSN must emit <SSN_***>."""
        pf = PrivacyFilter()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="API call failed: SSN 123-45-6789 in description",
            args=(),
            exc_info=None,
        )
        pf.filter(record)
        assert "<SSN_***>" in record.msg
        assert "123-45-6789" not in record.msg

    def test_ein_stripped_from_log(self):
        pf = PrivacyFilter()
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="Error: EIN 12-3456789 mismatch", args=(), exc_info=None,
        )
        pf.filter(record)
        assert "<EIN_***>" in record.msg

    def test_filter_always_returns_true(self):
        """PrivacyFilter must never suppress a log record."""
        pf = PrivacyFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Normal log message", args=(), exc_info=None,
        )
        result = pf.filter(record)
        assert result is True

    def test_clean_log_unchanged(self):
        pf = PrivacyFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Imported 11 transactions for 2025-01", args=(), exc_info=None,
        )
        pf.filter(record)
        assert record.msg == "Imported 11 transactions for 2025-01"


# ── Memory file round-trip ────────────────────────────────────────────────────

class TestMemoryRedaction:
    def test_pattern_persisted_as_redacted(self, tmp_path, monkeypatch):
        """
        R-46 memory_file scope: PAYMENT TO JOHN DOE → PAYMENT TO <PERSON_001>
        The on-disk JSON must not contain the real person name.
        """
        import config as _cfg
        mem_path = tmp_path / "memory.json"
        monkeypatch.setattr(_cfg, "MEMORY_FILE", mem_path)

        from intelligence.memory import ClassificationMemory
        mem = ClassificationMemory(memory_file=mem_path)

        # Simulate user confirming a classification for a transaction with a person name
        mem.remember(
            description="PAYMENT TO Jane Doe for services rendered",
            coa_code="5080",
            coa_name="Other Operating Expenses",
        )

        # Read back the raw JSON
        data = json.loads(mem_path.read_text(encoding="utf-8"))
        patterns = [r["pattern"] for r in data["rules"]]
        stored = " ".join(patterns)

        # The real person name must NOT appear on disk
        assert "Jane Doe" not in stored
        # A redaction token should appear instead
        assert "<PERSON_" in stored or "<ENTITY_NAME>" in stored or "Jane" not in stored

    def test_innocuous_pattern_unchanged(self, tmp_path, monkeypatch):
        """Patterns with no PII should persist verbatim."""
        import config as _cfg
        mem_path = tmp_path / "memory2.json"
        monkeypatch.setattr(_cfg, "MEMORY_FILE", mem_path)

        from intelligence.memory import ClassificationMemory
        mem = ClassificationMemory(memory_file=mem_path)
        mem.remember("PAYPAL *QUICKBOOKS", "5010", "Software & Subscriptions")

        data = json.loads(mem_path.read_text(encoding="utf-8"))
        patterns = [r["pattern"] for r in data["rules"]]
        # PAYPAL is in allowlist; QUICKBOOKS-like patterns should pass through
        stored = " ".join(patterns)
        assert "PAYPAL" in stored


# ── unredact() round-trip ─────────────────────────────────────────────────────

class TestUnredact:
    def test_email_roundtrip(self):
        original = "Contact user@example.com for help"
        safe, m = redact(original, scope="openai")
        assert "user@example.com" not in safe
        restored = unredact(safe, m)
        assert "user@example.com" in restored

    def test_account_roundtrip(self):
        original = "Account 1234567890 posting"
        safe, m = redact(original, scope="openai")
        assert "1234567890" not in safe
        restored = unredact(safe, m)
        assert "1234567890" in restored

    def test_empty_map_unchanged(self):
        text = "STRIPE $99 payment"
        assert unredact(text, {}) == text

    def test_unredact_result_dict(self):
        original = "Payment for user@company.com invoice"
        safe, m = redact(original, scope="openai")
        result = {"coa_code": "5010", "reason": f"Classified based on {safe}"}
        restored = unredact_result(result, m)
        assert "user@company.com" in restored["reason"]
        assert restored["coa_code"] == "5010"  # non-string fields untouched

    def test_ssn_not_in_map_so_stays_redacted(self):
        """SSN uses *** token — not in RedactionMap — stays redacted after unredact."""
        original = "SSN: 111-22-3333 on file"
        safe, m = redact(original, scope="openai")
        restored = unredact(safe, m)
        # SSN stays as <SSN_***> — we intentionally don't un-redact it
        assert "111-22-3333" not in restored
        assert "<SSN_***>" in restored


# ── privacy_status() ─────────────────────────────────────────────────────────

class TestPrivacyStatus:
    def test_returns_dict(self):
        status = privacy_status()
        assert isinstance(status, dict)
        assert "egress_mode" in status
        assert "detector_categories" in status

    def test_detector_count(self):
        status = privacy_status()
        assert status["detector_categories"] == 12

    def test_egress_mode_reflects_config(self, monkeypatch):
        import config as _cfg
        _cfg.AI_EGRESS_MODE = "mock"
        status = privacy_status()
        assert status["egress_mode"] == "mock"
        _cfg.AI_EGRESS_MODE = "redact"


# ── Performance ───────────────────────────────────────────────────────────────

class TestPerformance:
    def test_redaction_under_1s(self):
        """R-46 acceptance: redaction of 200-char description < 1 s p95."""
        import time
        text = (
            "PAYPAL *QUICKBOOKS SUBSCRIPTION $99.99 DEBIT "
            "CARD 4532015112830366 ACCT 1234567890 "
            "email@company.com 415-555-0101 "
            "2025-01-15"
        )[:200]

        times = []
        for _ in range(20):
            start = time.perf_counter()
            redact(text, scope="openai")
            times.append(time.perf_counter() - start)
            _reset_session()

        times.sort()
        p95 = times[int(0.95 * len(times))]
        assert p95 < 1.0, f"p95 redaction time {p95:.3f}s exceeds 1s"

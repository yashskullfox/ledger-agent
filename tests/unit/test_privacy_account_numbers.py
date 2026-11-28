"""
tests/unit/test_privacy_account_numbers.py  –  P-05 account-number firewall

Goal 7: no account number — full, partial, masked or last-4 — reaches an LLM or
leaves the host.  Covers core/privacy.py:

* marker-anchored partial / masked forms (x1234, *1234, acct ending 1234, …)
  in every egress scope → digit-free ``<ACCT_nnn>`` token;
* bare 8+ digit runs, plain and grouped, without any keyword;
* negatives: dates, amounts, years, short invoice numbers, ISO / compact
  timestamps, hex ids;
* one stable token per original within a session;
* audit_egress() blocks every form, naming the category only;
* non-ASCII context;
* the marker grammar shared with intelligence/owner_draws.py.

All digits below are synthetic.
"""
from __future__ import annotations

import re

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import privacy
from ledger_agent.core.intelligence import owner_draws
from ledger_agent.core.privacy import (
    PrivacyLeakError,
    audit_egress,
    contains_account_reference,
    redact,
    unredact,
)

LAST4 = "4821"
FULL = "918273645500"  # redaction: allow (synthetic)
GROUPED_SPACE = "9182 7364 5500"  # redaction: allow (synthetic)
GROUPED_HYPHEN = "9182-7364-5500"  # redaction: allow (synthetic)

EGRESS_SCOPES = [
    "openai", "openai_compatible", "gemini", "anthropic", "ollama",
    "ai_classification", "ai_context", "mcp_response", "memory_file",
    "unlisted_scope",  # unknown scopes fall back to every detector
]

# (id, text, digits that must disappear)
MASKED_FORMS = [
    ("x_lower", f"ONLINE TRANSFER FROM CHK x{LAST4}"),
    ("x_upper", f"TRANSFER TO X{LAST4}"),
    ("xxxx", f"Transfer to XXXX{LAST4}"),
    ("xxxx_hyphen", f"Transfer to XXXX-{LAST4}"),
    ("star", f"Card *{LAST4} autopay"),
    ("stars", f"CARD AUTOPAY ****{LAST4} THANK YOU"),
    ("stars_space", f"Card ** {LAST4} autopay"),
    ("dots", f"Payment to ...{LAST4}"),
    ("ellipsis", f"Payment to …{LAST4}"),
    ("acct", f"Transfer to acct {LAST4}"),
    ("acct_hash", f"Transfer to ACCT# {LAST4}"),
    ("acct_no", f"Transfer to acct no. {LAST4}"),
    ("account_ending", f"Transfer to account ending {LAST4}"),
    ("account_ending_in", f"Transfer to Account Ending In {LAST4}"),
    ("ending_in", f"Transfer to checking ending in {LAST4}"),
    ("card_ending", f"Payment card ending {LAST4}"),
    ("bullets", f"Card ••••{LAST4}"),
    ("last_four", f"Card last 4 digits {LAST4}"),
]

BARE_FORMS = [
    ("plain_12", f"Pago de software {FULL}"),
    ("plain_8", "Ref 73645512 wire"),  # redaction: allow (synthetic)
    ("plain_17", "Wire 91827364550012345 in"),  # redaction: allow (synthetic)
    ("grouped_space", f"Deposit to {GROUPED_SPACE} ok"),
    ("grouped_hyphen", f"Deposit to {GROUPED_HYPHEN} ok"),
    ("glued_to_word", f"REF{FULL} ACH"),
    ("long_run_before_decimal", "Ref 9182736455001.5 ok"),  # redaction: allow (synthetic)
]

# Text that must come out of redact() unchanged (scope ai_classification).
NEGATIVES = [
    ("year", "Fiscal year 2025 summary"),
    ("period_ending_year", "Period ending 2025"),
    ("date_iso", "Posted 2026-01-15 ok"),
    ("date_us", "Posted 01/15/2026 ok"),
    ("date_compact_ymd", "Statement 20260115 ok"),
    ("date_compact_mdy", "Statement 01152026 ok"),
    ("date_compact_hyphen", "Statement 2026-0115 ok"),
    ("iso_timestamp", "ts 2026-01-15T10:30:00Z"),
    ("iso_timestamp_frac", "ts 2026-11-23T21:56:44.471290+00:00"),
    ("compact_timestamp", "run 20260115103045 done"),  # redaction: allow (synthetic)
    ("compact_timestamp_12", "run 202601151030 done"),  # redaction: allow (synthetic)
    ("run_id", "run 20261123T215604Z-48b252"),
    ("amount_small", "Amount 1234.56 debit"),
    ("amount_large", "Amount 12345678.90 debit"),
    ("amount_comma", "Betrag 12345678,90 EUR"),
    ("amount_thousands", "Amount 12,345,678.00"),
    ("amount_currency", "Wire $12345678 total"),
    ("invoice_short", "INV 4821 paid"),
    ("invoice_7", "Invoice 1234567 paid"),
    ("check_number", "Check #4821"),
    ("two_numbers", "Invoice 2026 1500"),
    ("uuid", "id 4fde3df7-1881-4234-a567-b16d4bb3a612 ok"),
    ("hex_digest", "sha 4fde3df71881234567b16d4bb3a6 ok"),  # redaction: allow (synthetic)
    ("model_id", "claude-3-5-sonnet-20241022"),
    ("tax_word", "TAX4821 code"),
]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    privacy._reset_session()
    yield
    privacy._reset_session()


_TOKEN_RE = re.compile(r"<(?:ACCT|CARD)_\d{3}>")


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s)


def _assert_digit_free(out: str, original_digits: str) -> None:
    """No run of the original digits (or its last 4) survives, tokens included."""
    stripped = _TOKEN_RE.sub("", out)
    assert original_digits[-4:] not in stripped
    assert original_digits[-4:] not in out  # tokens carry no digit of the original
    assert _TOKEN_RE.search(out)


# ── masked / partial forms ───────────────────────────────────────────────────

@pytest.mark.parametrize("scope", EGRESS_SCOPES)
@pytest.mark.parametrize("form_id, text", MASKED_FORMS, ids=[f[0] for f in MASKED_FORMS])
def test_masked_form_becomes_digit_free_token(scope, form_id, text):
    out, mapping = redact(text, scope=scope)
    _assert_digit_free(out, LAST4)
    assert contains_account_reference(out) is None
    assert any(k.startswith("<ACCT_") for k in mapping)


def test_masked_token_replaces_marker_and_digits():
    out, _ = redact(f"CARD AUTOPAY ****{LAST4} THANK YOU", scope="ai_classification")
    assert "****" not in out
    assert "<ACCT_001>" in out


# ── bare digit runs ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("scope", EGRESS_SCOPES)
@pytest.mark.parametrize("form_id, text", BARE_FORMS, ids=[f[0] for f in BARE_FORMS])
def test_bare_digit_run_becomes_digit_free_token(scope, form_id, text):
    out, _ = redact(text, scope=scope)
    _assert_digit_free(out, _digits_only(text))
    assert not re.search(r"\d{8,}", _TOKEN_RE.sub("", out))
    assert contains_account_reference(out) is None


def test_full_number_with_keyword_is_digit_free():
    out, _ = redact(f"ACH DEBIT ACCOUNT {FULL} ONLINE", scope="openai")
    _assert_digit_free(out, FULL)


def test_luhn_card_token_is_digit_free():
    card = "4532015112830366"  # redaction: allow (Luhn-valid test PAN)
    out, _ = redact(f"Charged to {card}", scope="openai")
    assert "<CARD_001>" in out
    assert "0366" not in out


def test_phone_with_context_keeps_phone_token():
    out, _ = redact("call 555-123-4567 for help", scope="openai")
    assert "<PHONE_" in out and "<ACCT_" not in out


def test_log_scope_has_full_account_firewall():
    """P-15: ``log`` now runs the bare-run and masked-reference detectors too.

    Before P-15 this test asserted that ``log`` (stderr / audit files) left a
    bare 8-digit run unchanged.  stderr is inherited by the Java webapp (P-14),
    so it is egress: a bare run and a masked reference are tokenised in ``log``
    exactly as in the egress scopes; the negatives (dates, amounts, years) still
    pass through.
    """
    out, _ = redact(f"ACH DEBIT ACCOUNT {FULL}", scope="log")
    _assert_digit_free(out, FULL)
    out, _ = redact("event 73645512 done", scope="log")  # redaction: allow
    assert "73645512" not in out and "<ACCT_" in out  # redaction: allow
    out, _ = redact(f"paid from x{LAST4}", scope="log")
    _assert_digit_free(out, LAST4)
    for text in ("statement 20260115 closed", "total $12345678.90", "year 2026"):  # redaction: allow
        assert redact(text, scope="log")[0] == text


# ── negatives ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("neg_id, text", NEGATIVES, ids=[n[0] for n in NEGATIVES])
def test_negative_left_unchanged(neg_id, text):
    out, _ = redact(text, scope="ai_classification")
    assert "<ACCT_" not in out
    assert contains_account_reference(text) is None


# ── stable tokens ────────────────────────────────────────────────────────────

def test_same_original_same_token_within_session():
    a, _ = redact(f"Pago {FULL}", scope="openai")
    b, _ = redact(f"Refund {FULL} done", scope="mcp_response")
    c, _ = redact(f"Deposit {GROUPED_SPACE} x", scope="ai_context")
    tok = _TOKEN_RE.search(a).group(0)
    assert tok in b and tok in c


def test_masked_forms_of_same_last4_share_token():
    outs = [redact(t, scope="openai")[0] for _id, t in MASKED_FORMS[:10]]
    tokens = {_TOKEN_RE.search(o).group(0) for o in outs}
    assert len(tokens) == 1


def test_different_originals_get_different_tokens():
    out, _ = redact(f"from x{LAST4} to x1357", scope="openai")
    assert len(set(_TOKEN_RE.findall(out))) == 2


def test_unredact_round_trip():
    text = f"Transfer {FULL} then ****{LAST4}"
    safe, mapping = redact(text, scope="ai_classification")
    assert FULL not in safe and LAST4 not in safe
    assert unredact(safe, mapping) == text


def test_token_does_not_retrigger_detectors():
    safe, _ = redact(f"x{LAST4} and {FULL}", scope="openai")
    again, _ = redact(safe, scope="openai")
    assert again == safe
    audit_egress({"messages": [{"content": safe}]})


# ── audit_egress ─────────────────────────────────────────────────────────────

AUDIT_CASES = [(f"masked-{i}", t, "account_reference") for i, t in MASKED_FORMS] + [
    (f"bare-{i}", t, "digit_run") for i, t in BARE_FORMS
] + [("old_token_format", f"Paid <ACCT_xxx{LAST4}>", "account_reference")]


@pytest.mark.parametrize("case_id, text, category", AUDIT_CASES,
                         ids=[c[0] for c in AUDIT_CASES])
def test_audit_egress_blocks_without_echo(case_id, text, category):
    payload = {"model": "m", "messages": [{"role": "user", "content": text}]}
    with pytest.raises(PrivacyLeakError) as exc:
        audit_egress(payload)
    msg = str(exc.value)
    assert category in msg
    assert LAST4 not in msg
    for run in re.findall(r"\d{4,}", text):
        assert run not in msg


def test_audit_egress_blocks_plain_string_payload():
    with pytest.raises(PrivacyLeakError, match="digit_run"):
        audit_egress(f"Pago de software {FULL}")


@pytest.mark.parametrize("neg_id, text", NEGATIVES, ids=[n[0] for n in NEGATIVES])
def test_audit_egress_allows_negatives(neg_id, text):
    audit_egress({"messages": [{"content": text}]})


def test_audit_egress_allows_real_prompt_payload():
    from ledger_agent.core.intelligence.ai_backend._remote import (
        CLASSIFY_SYSTEM_PROMPT,
        build_classify_user_message,
    )

    payload = {
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 1024,
        "system": CLASSIFY_SYSTEM_PROMPT,
        "messages": [{"role": "user",
                      "content": build_classify_user_message("<ACCT_001> fee", -12345678.9)}],
    }
    audit_egress(payload)


def test_audit_egress_unicode_escape_is_not_a_digit_run():
    # json.dumps escapes "—" as —; "—2025" must not look like "20142025".
    audit_egress({"content": "Q4—2025 summary"})


# ── non-ASCII context ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    f"Überweisung an Konto ****{LAST4}",
    f"Überweisung Konto-Nr. {LAST4}",
    f"Pago a cuenta {LAST4}",
    f"Virement compte ...{LAST4}",
    f"口座 ****{LAST4} への振込",
    f"खाता x{LAST4} को भुगतान",
    f"Überweisung {FULL} erhalten",
])
def test_non_ascii_context(text):
    for scope in ("ai_classification", "anthropic", "mcp_response"):
        out, _ = redact(text, scope=scope)
        _assert_digit_free(out, LAST4 if LAST4 in text else FULL)
    with pytest.raises(PrivacyLeakError):
        audit_egress({"content": text})


# ── shared grammar with owner_draws ──────────────────────────────────────────

def test_owner_draws_uses_shared_grammar():
    assert owner_draws._ACCOUNT_REF_RE is privacy.ACCOUNT_REF_RE


@pytest.mark.parametrize("form_id, text", MASKED_FORMS, ids=[f[0] for f in MASKED_FORMS])
def test_firewall_is_superset_of_owner_draws(form_id, text):
    """Every reference owner_draws sees is also caught by the firewall."""
    if owner_draws.account_refs(text):
        assert contains_account_reference(text) == "account_reference"


def test_old_format_token_is_re_redacted():
    """Compatibility: a pattern saved with the pre-P-05 token format (e.g. in a
    memory file) still loads as plain text; if it is ever re-sent, its last-4 is
    stripped instead of leaking or crashing."""
    old = f"ACH fee <ACCT_xxx{LAST4}>"
    out, _ = redact(old, scope="memory_file")
    assert LAST4 not in out
    out, _ = redact(old, scope="anthropic")
    assert LAST4 not in out

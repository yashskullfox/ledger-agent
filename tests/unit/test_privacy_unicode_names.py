"""
tests/unit/test_privacy_unicode_names.py — P-07: person / counterparty names in
any language must not reach an LLM.

Covers the Unicode-aware name heuristics in ``ledger_agent/core/privacy.py``:
cased scripts (German, Spanish, French, Portuguese, Cyrillic) after payee /
transfer context words, non-cased scripts (Japanese, Chinese, Korean, Hindi,
Arabic) next to payment words, per-word allowlist semantics, and the negatives
that keep the classify prompt useful (vendors, allowlisted words, COA and
German bookkeeping vocabulary).  Synthetic names only; assertion messages never
echo a name.
"""
from __future__ import annotations

import json
import logging
import os
import re

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import privacy
from ledger_agent.core.privacy import audit_egress, redact

SCOPE = "ai_classification"
TOKEN_RE = re.compile(r"<(PERSON|COUNTERPARTY)_\d{3}>")


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    for key in list(os.environ):
        if key.startswith("FI_PARTNER_") and key.endswith("_NAME"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FI_DB_PATH", str(tmp_path / "absent" / "none.db"))
    privacy._reset_session()
    privacy.refresh_protected_names()
    yield
    privacy._reset_session()
    privacy.refresh_protected_names()


def _safe(text: str, scope: str = SCOPE) -> str:
    return redact(text, scope=scope)[0]


def _assert_gone(out: str, *needles: str) -> None:
    folded = out.casefold()
    hits = sum(1 for n in needles if n.casefold() in folded)
    assert hits == 0, f"{hits} name part(s) left in redacted text"


# ── Cased scripts: names after payee / transfer context words ─────────────────

CASED = [
    # (description, name parts that must disappear, context that must remain)
    ("Zahlung an Jürgen Müller", ("Jürgen", "Müller"), "Zahlung an"),
    ("Überweisung an Jörg Weißmann", ("Jörg", "Weißmann"), "Überweisung an"),
    ("GUTSCHRIFT VON JÜRGEN MÜLLER", ("JÜRGEN", "MÜLLER"), "GUTSCHRIFT VON"),
    ("Empfänger: Anja Größlinger", ("Anja", "Größlinger"), "Empfänger:"),
    ("Pago a José Núñez", ("José", "Núñez"), "Pago a"),
    ("Transferencia de María José de la Peña", ("María", "José", "Peña"),
     "Transferencia de"),
    ("Virement à Élodie Durand", ("Élodie", "Durand"), "Virement à"),
    ("Bénéficiaire : Gaëlle Lefèvre", ("Gaëlle", "Lefèvre"), "Bénéficiaire"),
    ("Pagamento para João Conceição", ("João", "Conceição"), "Pagamento para"),
    ("Pix para Ana Sampaiozinha", ("Ana", "Sampaiozinha"), "Pix para"),
    ("Перевод для Иван Петров", ("Иван", "Петров"), "Перевод для"),
    ("Получатель: Ольга Смирнова", ("Ольга", "Смирнова"), "Получатель"),
    ("PAY TO THE ORDER OF Casey Exampleperson", ("Casey", "Exampleperson"),
     "PAY TO THE ORDER OF"),
    ("Mr. Jordan Q. Sampleperson", ("Jordan", "Sampleperson"), "Mr."),
]


@pytest.mark.parametrize("text, parts, context", CASED,
                         ids=[f"cased_{i}" for i in range(len(CASED))])
def test_cased_script_names_redacted(text, parts, context):
    out = _safe(text)
    _assert_gone(out, *parts)
    assert TOKEN_RE.search(out)
    assert context in out, "context words must survive (classification signal)"


def test_mixed_case_english_person_after_zelle_payment_to():
    out = _safe("Zelle payment to Jordan Sampleperson")
    _assert_gone(out, "Jordan", "Sampleperson")
    assert out.startswith("Zelle payment to <PERSON_")


def test_all_caps_cyrillic_run_is_a_counterparty():
    out = _safe("ПЕРЕВОД ИВАН ПЕТРОВ")
    _assert_gone(out, "ИВАН", "ПЕТРОВ")
    assert out.startswith("ПЕРЕВОД <COUNTERPARTY_")


def test_all_caps_german_run_with_umlauts_and_eszett():
    out = _safe("LASTSCHRIFT STRAßMÜLLER BÄCKEREI")
    _assert_gone(out, "STRAßMÜLLER")
    assert "LASTSCHRIFT" in out


def test_company_suffix_gives_counterparty_not_person():
    out = _safe("Überweisung an Quillfeder Werkstatt GmbH")
    _assert_gone(out, "Quillfeder")
    assert "<COUNTERPARTY_" in out and "<PERSON_" not in out


# ── Non-cased scripts: names next to payment / transfer words ─────────────────

UNCASED = [
    ("振込 山田太郎", ("山田太郎",), "振込"),
    ("山田太郎への振込", ("山田太郎",), "への振込"),
    ("振込 ヤマダ タロウ", ("ヤマダ", "タロウ"), "振込"),
    ("山田花子様から入金", ("山田花子",), "入金"),
    ("转账给张伟", ("张伟",), "转账给"),
    ("김민수에게 송금", ("김민수",), "송금"),
    ("भुगतान राहुल शर्मा", ("राहुल", "शर्मा"), "भुगतान"),
    ("राहुल शर्मा को भुगतान", ("राहुल", "शर्मा"), "को भुगतान"),
    ("تحويل إلى أحمد علي", ("أحمد", "علي"), "تحويل"),
    ("Payment to राहुल शर्मा", ("राहुल", "शर्मा"), "Payment to"),
    ("Überweisung an 山田太郎", ("山田太郎",), "Überweisung an"),
]


@pytest.mark.parametrize("text, parts, context", UNCASED,
                         ids=[f"uncased_{i}" for i in range(len(UNCASED))])
def test_uncased_script_names_redacted(text, parts, context):
    out = _safe(text)
    _assert_gone(out, *parts)
    assert "<PERSON_" in out
    assert context in out


# ── Allowlist: an allowlisted word is exempt by itself only ──────────────────

@pytest.mark.parametrize("text, kept, parts", [
    ("ZELLE PAYMENT TO JORDAN SAMPLEPERSON", ("ZELLE", "PAYMENT TO"),
     ("JORDAN", "SAMPLEPERSON")),
    ("CASH APP PAYMENT TO JORDAN SAMPLEPERSON", ("CASH APP", "PAYMENT TO"),
     ("JORDAN", "SAMPLEPERSON")),
    ("VENMO CASEY EXAMPLEPERSON", ("VENMO",), ("CASEY", "EXAMPLEPERSON")),
    ("PAYPAL *JORDANSAMPLE", ("PAYPAL",), ("JORDANSAMPLE",)),
    ("STRIPE TRANSFER FROM QUILLFEDER", ("STRIPE", "TRANSFER FROM"), ("QUILLFEDER",)),
])
def test_allowlist_neighbours_still_evaluated(text, kept, parts):
    out = _safe(text)
    _assert_gone(out, *parts)
    for word in kept:
        assert word in out, "allowlisted / context word must be kept"


def test_multi_word_allowlist_entry_is_exempt_only_as_a_phrase():
    assert _safe("WELLS FARGO ONLINE TRANSFER") == "WELLS FARGO ONLINE TRANSFER"
    # One word of a multi-word entry is not exempt on its own.
    assert "<COUNTERPARTY_" in _safe("WELLS PAYMENT")


def test_classify_words_is_per_word():
    classes = privacy._classify_words(["ZELLE", "PAYMENT", "TO", "JORDAN", "SAMPLEPERSON"])
    assert classes == ["exempt", "exempt", "exempt", "name", "name"]
    assert privacy._classify_words(["CASH", "APP"]) == ["exempt", "exempt"]
    assert privacy._classify_words(["de", "GmbH", "A"]) == ["particle", "neutral", "exempt"]


# ── Negatives: vocabulary needed for classification stays ────────────────────

KEPT = [
    # allowlisted vendors and payment apps
    "STRIPE PAYMENT 1234",
    "PAYPAL CHARGE",
    "AMAZON MKTPLACE PMTS",
    "Payment to Amazon",
    "ZELLE FROM CHK",
    # local classifier vendor vocabulary and COA vocabulary (English)
    "QUICKBOOKS PAYROLL INTUIT",
    "YOUTUBE PREMIUM",
    "MARGIN INTEREST",
    "Transfer to Business Checking",
    "TRANSFER TO SAVINGS",
    "DIVIDEND REINVEST",
    "Interest Income credit",
    "CARD AUTOPAY THANK YOU",
    # German bookkeeping vocabulary
    "Zahlung an Finanzamt",
    "Miete für Büro",
    "Überweisung Büromaterial GmbH Rechnung",
    "LASTSCHRIFT KRANKENKASSE BEITRAG",
    "Gutschrift von Zinsen",
    # Spanish / French / Portuguese / Russian vocabulary
    "Pago de suscripción de software",
    "Pago de Nómina",
    "Virement de Salaire",
    "Pagamento para Fatura",
    "Абонентская плата за ПО",
    # non-cased compounds without a particle, and vocabulary
    "東京電力 電気料金 2月分",
    "家賃振込",
    "振込手数料",
    "किराया भुगतान",
    "भुगतान बिजली बिल",
    # prose without a context word
    "Description of transaction type",
    "Jörg Weißmann",
]


@pytest.mark.parametrize("text", KEPT, ids=[f"kept_{i}" for i in range(len(KEPT))])
def test_vocabulary_is_not_redacted(text):
    assert _safe(text) == text


def test_numbers_untouched():
    text = "Pago a José Núñez 1.234,56 EUR 2026-01-15 ref 4711"
    out = _safe(text)
    _assert_gone(out, "José", "Núñez")
    for part in ("1.234,56", "EUR", "2026-01-15", "4711"):
        assert part in out


def test_name_replaced_only_at_word_boundaries():
    # The detected name "Lima" must not be replaced inside longer words.
    out = _safe("Pix para Lima; ParaLimaX ALimaB")
    assert out.endswith("; ParaLimaX ALimaB")
    _assert_gone(out.split(";")[0], "Lima")


# ── Stable tokens ─────────────────────────────────────────────────────────────

def test_tokens_are_stable_and_case_insensitive():
    a = _safe("Zahlung an Jürgen Müller")
    b = _safe("GUTSCHRIFT VON JÜRGEN MÜLLER")
    c = _safe("Pago a José Núñez")
    ta, tb, tc = (TOKEN_RE.search(x).group(0) for x in (a, b, c))
    assert ta == tb
    assert ta != tc
    assert re.fullmatch(r"<PERSON_\d{3}>", ta)


def test_uncased_tokens_are_stable():
    a = _safe("振込 山田太郎")
    b = _safe("山田太郎への振込")
    assert TOKEN_RE.search(a).group(0) == TOKEN_RE.search(b).group(0)


def test_unredact_restores_the_name_locally():
    text = "Zahlung an Jürgen Müller"
    safe, mapping = redact(text, scope=SCOPE)
    assert privacy.unredact(safe, mapping) == text


def test_no_name_in_logs(caplog):
    with caplog.at_level(logging.DEBUG, logger="fi.privacy"):
        _safe("Zahlung an Jürgen Müller")
        _safe("राहुल शर्मा को भुगतान")
    logged = " ".join(r.getMessage() + json.dumps(r.__dict__, default=str)
                      for r in caplog.records)
    _assert_gone(logged, "Jürgen", "Müller", "राहुल")


def test_memory_file_scope_redacts_unicode_names():
    out = _safe("Zahlung an Jürgen Müller", scope="memory_file")
    _assert_gone(out, "Jürgen", "Müller")


# ── The classify prompt stays intact and passes audit_egress ─────────────────

def test_real_prompts_survive_redaction_unchanged():
    from ledger_agent.core.intelligence.ai_backend import _remote

    for prompt in (_remote.CLASSIFY_SYSTEM_PROMPT, _remote.ENHANCE_SYSTEM_PROMPT,
                   _remote.EXPLAIN_SYSTEM_PROMPT):
        assert _safe(prompt) == prompt


@pytest.mark.parametrize("desc", [
    "Zahlung an Jürgen Müller", "山田太郎への振込", "राहुल शर्मा को भुगतान",
    "ZELLE PAYMENT TO JORDAN SAMPLEPERSON",
])
def test_audit_egress_passes_real_classify_payload(desc):
    from ledger_agent.core.intelligence.ai_backend import _remote

    safe = _safe(desc)
    payload = {
        "system": _remote.CLASSIFY_SYSTEM_PROMPT,
        "messages": [{"role": "user",
                      "content": _remote.build_classify_user_message(safe, -42.0)}],
    }
    audit_egress(payload)  # must not raise
    assert TOKEN_RE.search(payload["messages"][0]["content"])

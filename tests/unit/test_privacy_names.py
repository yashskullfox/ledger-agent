"""
tests/unit/test_privacy_names.py — P-04: entity and partner names are always redacted.

Protected names come from the DB ``entities`` table (read-only) plus
FI_PRIVACY_ENTITY_NAME and FI_PARTNER_<n>_NAME, and are redacted in every
egress scope whether or not the caller passes ``entity_name=``.
Synthetic names only.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import config as _cfg
from ledger_agent.core import privacy
from ledger_agent.core.privacy import PrivacyLeakError, audit_egress, redact

ENTITY = "Northwind Quillfeather Group LLC"
PARTNER = "Alex Testpartner"
PARTNER_DE = "Jürgen Müller"
PARTNER_HI = "राहुल शर्मा"


def _make_db(path, *names: str) -> None:
    from ledger_agent.core.database import EntityRepo, init_db
    from ledger_agent.core.models import Entity

    init_db(path)
    for i, name in enumerate(names):
        EntityRepo.upsert(Entity(id=f"ent-{i}", name=name, entity_type="LLC", state="MO"), path)


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


@pytest.fixture
def entity_db(tmp_path, monkeypatch):
    db_file = tmp_path / "names.db"
    _make_db(db_file, ENTITY)
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    return db_file


# ── DB-sourced entity names ───────────────────────────────────────────────────

def test_db_entity_name_redacted_without_env(entity_db):
    out, mapping = redact(f"Capital contribution {ENTITY} Q1", scope="ai_classification")
    assert "northwind" not in out.casefold()
    assert "quillfeather" not in out.casefold()
    assert privacy.ENTITY_TOKEN in out
    assert mapping[privacy.ENTITY_TOKEN] == ENTITY


def test_db_entity_name_case_and_whitespace_insensitive(entity_db):
    out, _ = redact("WIRE FROM northwind   QUILLFEATHER\tgroup llc", scope="openai")
    assert "northwind" not in out.casefold()
    assert "quillfeather" not in out.casefold()


def test_db_entity_single_token_redacted(entity_db):
    out, _ = redact("Invoice paid to Quillfeather on account", scope="ai_classification")
    assert "quillfeather" not in out.casefold()
    assert privacy.ENTITY_TOKEN in out


@pytest.mark.parametrize("scope", [
    "openai", "openai_compatible", "gemini", "anthropic", "ollama",
    "ai_classification", "ai_context", "mcp_response", "memory_file", "some_new_scope",
])
def test_every_egress_scope_redacts_names(scope, entity_db, monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    out, _ = redact(f"{ENTITY} paid {PARTNER}", scope=scope)
    folded = out.casefold()
    assert "northwind" not in folded
    assert "testpartner" not in folded


def test_tokens_contain_no_part_of_name(entity_db, monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    out, mapping = redact(f"{ENTITY} reimbursed {PARTNER}", scope="ai_classification")
    for token in mapping:
        folded = token.casefold()
        for part in ("northwind", "quillfeather", "alex", "testpartner"):
            assert part not in folded
    assert "<PERSON_" in out


# ── Partner names from env ────────────────────────────────────────────────────

def test_partner_full_name_and_surname_token(monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    full, _ = redact(f"Reimbursement {PARTNER}", scope="ai_classification")
    surname, _ = redact("ZELLE FROM TESTPARTNER", scope="ai_classification")
    assert "testpartner" not in full.casefold()
    assert "alex" not in full.casefold()
    assert "testpartner" not in surname.casefold()


def test_partner_tokens_stable_per_person(monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    a, _ = redact(f"Draw {PARTNER}", scope="ai_classification")
    b, _ = redact("Draw TESTPARTNER", scope="ai_classification")
    assert a == b


def test_second_partner_redacted(monkeypatch):
    monkeypatch.setenv("FI_PARTNER_2_NAME", "Morgan Fakesurname")
    out, _ = redact("Payment to morgan fakesurname", scope="mcp_response")
    assert "fakesurname" not in out.casefold()


def test_word_boundary_respected(monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    out, _ = redact("Alexander Hamilton bridge toll", scope="ai_classification")
    assert out == "Alexander Hamilton bridge toll"


def test_non_ascii_partner_name(monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER_DE)
    full, _ = redact(f"Zahlung an {PARTNER_DE}", scope="ai_classification")
    upper, _ = redact("ÜBERWEISUNG MÜLLER", scope="ai_classification")
    assert "müller" not in full.casefold() and "jürgen" not in full.casefold()
    assert "müller" not in upper.casefold()


def test_devanagari_partner_name(monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER_HI)
    out, _ = redact(f"{PARTNER_HI} को भुगतान", scope="ai_classification")
    assert "शर्मा" not in out and "राहुल" not in out


def test_explicit_entity_name_argument_still_works():
    out, _ = redact("Deposit Zebulon Widgets LLC", scope="openai",
                    entity_name="Zebulon Widgets LLC")
    assert "zebulon" not in out.casefold()


# ── Generic words are not protected ───────────────────────────────────────────

def test_generic_words_not_redacted(entity_db):
    # "Group" and "LLC" are in ENTITY but are generic: other vendors keep them.
    out, _ = redact("Paid Acme Group invoice", scope="ai_classification")
    assert "Group" in out
    out, _ = redact("Annual LLC filing fee", scope="ai_classification")
    assert "LLC" in out


def test_generic_only_names_not_protected(monkeypatch):
    # A default-style partner label ("Partner A") is not identifying.
    monkeypatch.setenv("FI_PARTNER_1_NAME", "Partner A")
    out, _ = redact("Partner allocation note", scope="ai_classification")
    assert out == "Partner allocation note"


def test_placeholder_tokens_skipped(tmp_path, monkeypatch):
    db_file = tmp_path / "ph.db"
    _make_db(db_file, "ENTITY_A HOLDINGS LLC")
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    full, _ = redact("Contribution Entity_A Holdings LLC", scope="ai_classification")
    assert "entity_a" not in full.casefold()
    # the individual words are a placeholder + generic words → not protected
    alone, _ = redact("entity holdings report", scope="ai_classification")
    assert alone == "entity holdings report"


# ── audit_egress ──────────────────────────────────────────────────────────────

def test_audit_egress_blocks_protected_name_without_echo(entity_db, monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    for payload in (
        {"messages": [{"role": "user", "content": f"txn {ENTITY}"}]},
        {"messages": [{"role": "user", "content": "txn from TESTPARTNER"}]},
        f"raw text {PARTNER}",
    ):
        with pytest.raises(PrivacyLeakError) as exc:
            audit_egress(payload)
        msg = str(exc.value).casefold()
        assert "protected_name" in msg
        for part in ("northwind", "quillfeather", "alex", "testpartner"):
            assert part not in msg


def test_audit_egress_blocks_non_ascii_name_in_dict(monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER_DE)
    with pytest.raises(PrivacyLeakError):
        audit_egress({"content": "Zahlung MÜLLER"})


def test_audit_egress_passes_redacted_payload(entity_db, monkeypatch):
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    safe, _ = redact(f"{ENTITY} paid {PARTNER}", scope="openai")
    audit_egress({"messages": [{"role": "user", "content": safe}]})


# ── Cache behaviour and DB safety ─────────────────────────────────────────────

def test_cache_follows_fi_db_path(tmp_path, monkeypatch):
    db1, db2 = tmp_path / "one.db", tmp_path / "two.db"
    _make_db(db1, "Quillfeather Corp")
    _make_db(db2, "Brambleton Corp")
    monkeypatch.setenv("FI_DB_PATH", str(db1))
    a, _ = redact("Quillfeather and Brambleton", scope="ai_classification")
    assert "Quillfeather" not in a and "Brambleton" in a
    monkeypatch.setenv("FI_DB_PATH", str(db2))
    b, _ = redact("Quillfeather and Brambleton", scope="ai_classification")
    assert "Quillfeather" in b and "Brambleton" not in b


def test_new_entity_picked_up_after_write_or_refresh(tmp_path, monkeypatch):
    from ledger_agent.core.database import EntityRepo
    from ledger_agent.core.models import Entity

    db_file = tmp_path / "grow.db"
    _make_db(db_file, "Quillfeather Corp")
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    redact("warm cache", scope="ai_classification")
    EntityRepo.upsert(Entity(id="ent-new", name="Brambleton Corp", entity_type="LLC",
                             state="MO"), db_file)
    privacy.refresh_protected_names()
    out, _ = redact("Brambleton deposit", scope="ai_classification")
    assert "Brambleton" not in out


def test_missing_db_env_only_and_not_created(tmp_path, monkeypatch):
    missing = tmp_path / "nodir" / "missing.db"
    monkeypatch.setenv("FI_DB_PATH", str(missing))
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    out, _ = redact(f"Draw {PARTNER} Quillfeather", scope="ai_classification")
    assert "testpartner" not in out.casefold()
    assert "Quillfeather" in out  # no DB → no entity names
    assert not missing.exists()
    assert not missing.parent.exists()


def test_unreadable_db_falls_back_to_env(tmp_path, monkeypatch):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite database at all" * 10)
    monkeypatch.setenv("FI_DB_PATH", str(bad))
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    out, _ = redact(f"Draw {PARTNER}", scope="ai_classification")
    assert "testpartner" not in out.casefold()


def test_db_without_entities_table(tmp_path, monkeypatch):
    db_file = tmp_path / "empty.db"
    sqlite3.connect(db_file).close()
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    assert privacy.get_protected_names() == []
    out, _ = redact("plain text", scope="ai_classification")
    assert out == "plain text"


def test_db_read_is_read_only(entity_db):
    before = entity_db.stat().st_mtime_ns
    redact(f"{ENTITY}", scope="ai_classification")
    assert entity_db.stat().st_mtime_ns == before


def test_relative_fi_db_path(tmp_path, monkeypatch):
    _make_db(tmp_path / "rel.db", ENTITY)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FI_DB_PATH", "rel.db")
    out, _ = redact(f"Deposit {ENTITY}", scope="ai_classification")
    assert "northwind" not in out.casefold()


def test_default_entity_placeholder_not_protected(tmp_path, monkeypatch):
    # api.py names a fresh entity "Entity" (FI_DEFAULT_ENTITY_NAME default); that
    # generic word must not be redacted from every payload.
    db_file = tmp_path / "default.db"
    _make_db(db_file, "Entity")
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    assert privacy.get_protected_names() == []

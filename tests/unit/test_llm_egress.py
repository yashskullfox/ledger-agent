"""
tests/unit/test_llm_egress.py  –  P-01 end-to-end egress tests (Stream P, privacy)

Goal 7: no account number (not even partial) and no entity / partner name ever
reaches an LLM or a server.  Every test here feeds a synthetic "leak case" into a
real egress path and asserts that none of the case's needles appears in what
actually leaves the process:

* every remote AI backend in ``ai_backend.PROVIDERS`` (openai, gemini, anthropic,
  openai_compatible, ollama) — the exact payload handed to a fake SDK client,
  for classify / explain / enhance;
* the ``adapters/context_builder.py`` AI context export;
* the legacy ``mcp_server/server.py`` tool responses;
* the ``ledger_agent/mcp/server.py`` tool responses on the default path
  (``_meta.allow_pii=true`` is an explicit opt-in and may carry PII).

Cases that leak today are ``xfail(strict=True)`` with the ID of the card that
fixes them (P-02 legacy MCP, P-04 entity/partner names, P-05 partial numbers,
P-06 fail-closed + context labels, P-07 Unicode/allowlist).  When a card lands,
its xfails turn into XPASS failures and must be removed — that is the point.

No network: provider SDKs are fakes injected through ``sys.modules``.
Synthetic tmp_path databases only; pseudonymous names; no real data.
"""
from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

import config as _cfg
from ledger_agent.core import privacy
from ledger_agent.core.intelligence import ai_backend

# ── Synthetic fixtures ────────────────────────────────────────────────────────

FY = 2026
PERIOD = f"{FY}-01"
ENTITY_ID = "ent-egress"
ENTITY_NAME = "ENTITY_A HOLDINGS LLC"
PARTNER_NAME = "Alex Testpartner"
# Synthetic identifiers (not Luhn-valid, not real).  Last-4 values are distinct
# across cases so one case never matches another case's text.
ACCT_NUMBER = "4400123456789"  # redaction: allow (synthetic)
BARE_DIGITS = "918273645500"  # redaction: allow (synthetic)
ACCT_MASK = "****5307"  # redaction: allow (synthetic; = masked_asterisk_last4)
GOOD_JSON = json.dumps({
    "coa_code": "5080",
    "coa_name": "Other Operating Expenses",
    "is_transfer": False,
    "confidence": 0.9,
    "reason": "synthetic",
})


@dataclass(frozen=True)
class Leak:
    id: str
    text: str            # transaction description fed into the egress path
    needles: tuple[str, ...]  # must not appear (compared with str.casefold())


LEAKS: list[Leak] = [
    Leak("acct_number_keyword", f"ACH DEBIT ACCOUNT {ACCT_NUMBER} ONLINE", (ACCT_NUMBER,)),
    Leak("acct_number_keyword_last4", f"ACH DEBIT ACCOUNT {ACCT_NUMBER} ONLINE",
         (ACCT_NUMBER[-4:],)),
    Leak("bare_digit_run", f"Pago de software {BARE_DIGITS}", (BARE_DIGITS,)),
    Leak("masked_x_last4", "ONLINE TRANSFER FROM CHK x4821", ("4821",)),
    Leak("masked_asterisk_last4", f"CARD AUTOPAY {ACCT_MASK} THANK YOU", (ACCT_MASK[-4:],)),
    Leak("acct_ending_last4", "ACH DEBIT acct ending 9164 utility", ("9164",)),
    Leak("entity_name", "Capital contribution Entity_A Holdings LLC", ("entity_a",)),
    Leak("partner_name", f"Reimbursement {PARTNER_NAME}", ("testpartner",)),
    Leak("non_ascii_german", "Zahlung an Jürgen Müller", ("jürgen", "müller")),
    Leak("non_ascii_spanish", "Pago a José Peña", ("josé", "peña")),
    Leak("non_ascii_japanese", "山田太郎への振込", ("山田太郎",)),
    Leak("non_ascii_hindi", "राहुल शर्मा को भुगतान", ("राहुल", "शर्मा")),
    Leak("allowlist_neighbour", "ZELLE PAYMENT TO JORDAN SAMPLEPERSON", ("sampleperson",)),
]
LEAK_BY_ID = {leak.id: leak for leak in LEAKS}

# ── Known gaps (each maps to the card that closes it) ─────────────────────────

# Gaps of privacy.redact() itself — inherited by every path that relies on it.
REDACT_GAPS: dict[str, str] = {
    "acct_number_keyword_last4":
        "P-05: account token <ACCT_xxxNNNN> keeps the last 4 digits",
    "bare_digit_run":
        "P-05: bare 8-17 digit run without an account keyword is not redacted",
    "masked_x_last4": "P-05: masked 'x'+last-4 form is not detected",
    "masked_asterisk_last4": "P-05: asterisk+last-4 form is not detected",
    "acct_ending_last4": "P-05: 'acct ending'+last-4 form is not detected",
    "non_ascii_german": "P-07: person/counterparty detection is ASCII-only",
    "non_ascii_spanish": "P-07: person/counterparty detection is ASCII-only",
    "non_ascii_japanese": "P-07: person/counterparty detection is ASCII-only",
    "non_ascii_hindi": "P-07: person/counterparty detection is ASCII-only",
    "allowlist_neighbour":
        "P-07: one allowlisted token exempts the whole all-caps run",
}

def _params(gaps: dict[str, str]) -> list:
    """One pytest param per leak case; known gaps become strict xfails."""
    out = []
    for leak in LEAKS:
        marks = []
        if leak.id in gaps:
            marks.append(pytest.mark.xfail(strict=True, reason=gaps[leak.id]))
        out.append(pytest.param(leak, id=leak.id, marks=marks))
    return out


def _assert_no_leak(leak: Leak, text: str) -> None:
    folded = text.casefold()
    hits = sum(1 for n in leak.needles if n.casefold() in folded)
    # Report the case id and a count only — never echo needles or payload.
    assert hits == 0, f"leak case {leak.id!r}: {hits} needle(s) present in egress"


# ── Common fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _privacy_env(monkeypatch):
    """Default redact mode, no FI_PRIVACY_ENTITY_NAME, synthetic partner name."""
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.setattr(_cfg, "OPENAI_BASE_URL", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER_NAME)
    privacy._reset_session()
    yield
    privacy._reset_session()


def _seed(db_file) -> None:
    """Entity + checking/brokerage accounts + one transaction per leak case."""
    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        PositionRepo,
        SnapshotRepo,
        TransactionRepo,
        init_db,
    )
    from ledger_agent.core.models import (
        Account,
        AccountSnapshot,
        AccountType,
        Entity,
        Position,
        Transaction,
        TransactionType,
    )

    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name=ENTITY_NAME, entity_type="LLC", state="MO"), db_file
    )
    AccountRepo.upsert(Account(
        id="acct-egress-chk", entity_id=ENTITY_ID, name="BANK_X",
        institution="BANK_X", account_type=AccountType.CHECKING,
        account_number_masked=ACCT_MASK,
    ), db_file)
    AccountRepo.upsert(Account(
        id="acct-egress-brk", entity_id=ENTITY_ID, name="BROKER_Y",
        institution="BROKER_Y", account_type=AccountType.BROKERAGE,
        account_number_masked="0000",
    ), db_file)
    TransactionRepo.bulk_insert([
        Transaction(
            id=f"txn-egress-{i}",
            account_id="acct-egress-chk",
            date=date(FY, 1, 5),
            description=leak.text,
            raw_description=leak.text,
            amount=Decimal("-42.00"),
            transaction_type=TransactionType.DEBIT,
            statement_period=PERIOD,
            coa_code="5080",  # classified → legacy list_transactions takes the raw-row path
            coa_name="Other Operating Expenses",
        )
        for i, leak in enumerate(LEAKS)
    ], db_file)
    for month in range(1, 13):
        for acct in ("acct-egress-chk", "acct-egress-brk"):
            SnapshotRepo.upsert(AccountSnapshot(
                account_id=acct, statement_period=f"{FY}-{month:02d}",
                ending_balance=Decimal("100"),
            ), db_file)
    PositionRepo.upsert_period([Position(
        account_id="acct-egress-brk", symbol="TICKER_SEC1", name="TICKER_SEC1",
        quantity=Decimal("1"), price_per_unit=Decimal("100"),
        market_value=Decimal("100"), statement_period=PERIOD,
    )], db_file)


@pytest.fixture(scope="module")
def _seeded_db_file(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("egress") / "egress.db"
    _seed(db_file)
    return db_file


@pytest.fixture
def seeded_db(_seeded_db_file, monkeypatch):
    """Point FI_DB_PATH at the shared synthetic DB (read-only use) for one test."""
    monkeypatch.setenv("FI_DB_PATH", str(_seeded_db_file))
    return _seeded_db_file


@pytest.fixture
def entity_db(tmp_path, monkeypatch):
    """Fresh tmp DB holding only the synthetic entity (for name-aware redaction)."""
    from ledger_agent.core.database import EntityRepo, init_db
    from ledger_agent.core.models import Entity

    db_file = tmp_path / "entity.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name=ENTITY_NAME, entity_type="LLC", state="MO"), db_file
    )
    return db_file


# ══════════════════════════════════════════════════════════════════════════════
# 1. Remote AI backends — exact payload handed to the provider SDK
# ══════════════════════════════════════════════════════════════════════════════

REMOTE_BACKENDS = sorted(set(ai_backend.PROVIDERS) - {"local"})


@pytest.fixture
def sent(monkeypatch) -> list[Any]:
    """Install fake provider SDKs; every outbound request is appended here."""
    sent: list[Any] = []

    class _FakeAnthropic:
        def __init__(self, **_kw: Any) -> None:
            self.messages = types.SimpleNamespace(create=self._create)

        @staticmethod
        def _create(**kwargs: Any):
            sent.append(kwargs)
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text=GOOD_JSON)],
            )

    class _FakeOpenAI:
        def __init__(self, **_kw: Any) -> None:
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self._create)
            )

        @staticmethod
        def _create(**kwargs: Any):
            sent.append(kwargs)
            msg = types.SimpleNamespace(content=GOOD_JSON)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    class _FakeGeminiModel:
        def __init__(self, **_kw: Any) -> None:
            pass

        @staticmethod
        def generate_content(payload: Any):
            sent.append(payload)
            return types.SimpleNamespace(text=GOOD_JSON)

    anthropic_mod = types.ModuleType("anthropic")
    anthropic_mod.Anthropic = _FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", anthropic_mod)

    openai_mod = types.ModuleType("openai")
    openai_mod.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
    openai_mod.RateLimitError = type("RateLimitError", (Exception,), {})  # type: ignore[attr-defined]
    openai_mod.APITimeoutError = type("APITimeoutError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", openai_mod)

    genai_mod = types.ModuleType("google.generativeai")
    genai_mod.configure = lambda **_kw: None  # type: ignore[attr-defined]
    genai_mod.GenerativeModel = _FakeGeminiModel  # type: ignore[attr-defined]
    google_mod = types.ModuleType("google")
    google_mod.generativeai = genai_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.generativeai", genai_mod)

    for var in ("FI_OPENAI_API_KEY", "FI_GEMINI_API_KEY", "FI_ANTHROPIC_API_KEY"):
        monkeypatch.setenv(var, "test-key-not-real")
    return sent


def _remote(name: str):
    """The remote half of ChainedBackend(local → remote) built by the registry."""
    backend = ai_backend.build_backend(name)
    return getattr(backend, "_remote", backend)


def _drive_all_methods(remote, text: str) -> None:
    remote.classify_transaction(text, -42.0)
    remote.explain_classification(text, "5080", "Other Operating Expenses")
    remote.enhance_memory_rule(text, "5080", 3)


def _dump(obj: Any) -> str:
    """Serialise a captured payload without \\u-escaping (escapes would hide leaks)."""
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, ensure_ascii=False, default=str)


@pytest.mark.parametrize("backend_name", REMOTE_BACKENDS)
def test_backend_payload_is_captured(backend_name, sent, entity_db):
    """Non-vacuity: every backend really sends classify/explain/enhance payloads."""
    remote = _remote(backend_name)
    assert remote.backend_name in ("openai", "gemini", "anthropic", "openai_compatible")
    _drive_all_methods(remote, "monthly software subscription")
    assert len(sent) == 3
    assert all("monthly software subscription" in _dump(p) for p in sent)


@pytest.mark.parametrize("leak", _params(REDACT_GAPS))
@pytest.mark.parametrize("backend_name", REMOTE_BACKENDS)
def test_backend_payload_has_no_leak(backend_name, leak, sent, entity_db):
    """Nothing identifying is in any request a remote backend hands to its SDK.

    A request blocked by audit_egress() is never sent, so it counts as no leak;
    test_backend_payload_is_captured proves benign requests do get through."""
    _drive_all_methods(_remote(backend_name), leak.text)
    _assert_no_leak(leak, "\n".join(_dump(p) for p in sent))


# ══════════════════════════════════════════════════════════════════════════════
# 2. adapters/context_builder.py — AI context export (P-06)
# ══════════════════════════════════════════════════════════════════════════════

CONTEXT_GAPS = {
    **REDACT_GAPS,
    "entity_name": "P-06: context export never redacts entity / account labels "
                   "(description part needs P-04)",
}


def _context_egress() -> str:
    from adapters.context_builder import build_context, context_to_prompt

    ctx = build_context(ENTITY_ID, period=PERIOD)
    return json.dumps(ctx, ensure_ascii=False, default=str) + "\n" + context_to_prompt(ctx)


def test_context_export_includes_redacted_transactions(seeded_db):
    """Non-vacuity: the export carries the seeded transactions and position."""
    from adapters.context_builder import build_context

    ctx = build_context(ENTITY_ID, period=PERIOD)
    assert len(ctx["transactions"]) == len(LEAKS)
    assert ctx["positions"]
    assert any("<ACCT_" in t["description"] for t in ctx["transactions"])


@pytest.mark.parametrize("leak", _params(CONTEXT_GAPS))
def test_context_export_has_no_leak(leak, seeded_db):
    _assert_no_leak(leak, _context_egress())


@pytest.mark.xfail(strict=True, reason="P-06: context_builder falls back to the raw "
                                       "description when redact() raises")
def test_context_export_fails_closed_on_redaction_error(seeded_db, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(privacy, "redact", _boom)
    try:
        text = _context_egress()
    except Exception:
        return  # aborting the export is an acceptable fail-closed outcome
    _assert_no_leak(LEAK_BY_ID["acct_number_keyword"], text)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Legacy mcp_server/server.py tool responses (P-02)
# ══════════════════════════════════════════════════════════════════════════════

def _decode_mcp(response: dict) -> str:
    """Flatten a JSON-RPC response, decoding JSON text content without \\u-escapes."""
    parts = [json.dumps(response, ensure_ascii=False)]
    for item in (response.get("result") or {}).get("content") or []:
        text = item.get("text", "")
        try:
            parts.append(json.dumps(json.loads(text), ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(text)
    return "\n".join(parts)


def _legacy_call(capsys, tool: str, args: dict) -> str:
    from mcp_server import server as legacy

    capsys.readouterr()
    legacy._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": tool, "arguments": args}})
    out = capsys.readouterr().out.strip().splitlines()
    assert out, "legacy server wrote no response"
    return "\n".join(_decode_mcp(json.loads(line)) for line in out)


# tool id → (tool name, arguments, gaps).  classify_transaction gets the leak text
# as its own input (P-02: it must not echo the description back).  The
# list_transactions paths return redacted descriptions, so they inherit the
# remaining privacy.redact() gaps (P-04/P-05/P-07).
LEGACY_TOOLS: dict[str, tuple[str, dict, dict[str, str]]] = {
    "get_balance_sheet": ("get_balance_sheet", {"period": PERIOD}, {}),
    "list_transactions_period": ("list_transactions", {"period": PERIOD}, REDACT_GAPS),
    "list_transactions_all": ("list_transactions", {}, REDACT_GAPS),
    "get_tax_estimate": ("get_tax_estimate", {"period": PERIOD}, {}),
    "classify_transaction": ("classify_transaction", {}, {}),
    "list_periods": ("list_periods", {}, {}),
    "get_entity_summary": ("get_entity_summary", {}, {}),
    "privacy_status": ("privacy_status", {}, {}),
}


def _legacy_params() -> list:
    out = []
    for tool_id, (_tool, _args, gaps) in LEGACY_TOOLS.items():
        for leak in LEAKS:
            marks = []
            if leak.id in gaps:
                marks.append(pytest.mark.xfail(strict=True, reason=gaps[leak.id]))
            out.append(pytest.param(tool_id, leak, id=f"{tool_id}-{leak.id}", marks=marks))
    return out


def test_legacy_tools_return_seeded_data(seeded_db, capsys):
    """Non-vacuity: the legacy tools see the synthetic DB."""
    from mcp_server import server as legacy

    assert set(LEGACY_TOOLS) >= {name for name in legacy._HANDLERS} - {"list_transactions"}
    text = _legacy_call(capsys, "list_transactions", {"period": PERIOD})
    assert f'"count": {len(LEAKS)}' in text
    assert '"isError"' not in _legacy_call(capsys, "get_balance_sheet", {"period": PERIOD})


@pytest.mark.parametrize("tool_id, leak", _legacy_params())
def test_legacy_mcp_response_has_no_leak(tool_id, leak, seeded_db, capsys):
    tool, args, _gaps = LEGACY_TOOLS[tool_id]
    if tool == "classify_transaction":
        args = {"description": leak.text, "amount": -42.0}
    _assert_no_leak(leak, _legacy_call(capsys, tool, args))


def test_legacy_error_has_no_traceback(seeded_db, capsys, monkeypatch):
    from mcp_server import server as legacy

    def _boom(_args):
        raise RuntimeError("synthetic failure")

    monkeypatch.setitem(legacy._HANDLERS, "list_periods", _boom)
    text = _legacy_call(capsys, "list_periods", {})
    assert "Traceback" not in text, "legacy error response carries a Python traceback"
    assert "synthetic failure" not in text
    assert "ref:" in text


def test_legacy_list_transactions_fails_closed(seeded_db, capsys, monkeypatch):
    """Fixed by P-02 (legacy server now redacts centrally and fails closed)."""
    def _boom(*_a, **_kw):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(privacy, "redact", _boom)
    text = _legacy_call(capsys, "list_transactions", {"period": PERIOD})
    _assert_no_leak(LEAK_BY_ID["acct_number_keyword"], text)
    assert "redaction_failed" in text


# ══════════════════════════════════════════════════════════════════════════════
# 4. ledger_agent/mcp/server.py tool responses (default path vs allow_pii)
# ══════════════════════════════════════════════════════════════════════════════

MCP_TOOLS: dict[str, dict] = {
    "generate_balance_sheet": {"fiscal_year": FY},
    "generate_form_1065": {"fiscal_year": FY},
    "generate_k1": {"fiscal_year": FY, "partner_id": "partner_1"},
    "pte_estimate": {"fiscal_year": FY},
    "customer_summary": {"fiscal_year": FY},
    "reconcile_year": {"fiscal_year": FY},
    "audit_year": {"fiscal_year": FY},
}


def _mcp_call(tool: str, args: dict, allow_pii: bool | None = None) -> dict:
    from ledger_agent.mcp.server import _dispatch

    params: dict[str, Any] = {"name": tool, "arguments": args}
    if allow_pii is not None:
        params["_meta"] = {"allow_pii": allow_pii}
    response = _dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": params})
    assert response is not None
    return response


def test_mcp_tool_list_covered():
    """Every non-import MCP tool is exercised below (import needs a statements folder)."""
    from ledger_agent.mcp.tools import TOOL_SCHEMAS

    assert {t["name"] for t in TOOL_SCHEMAS} - {"import_statements"} == set(MCP_TOOLS)


@pytest.mark.parametrize("allow_pii", [None, False], ids=["no_meta", "allow_pii_false"])
@pytest.mark.parametrize("tool", sorted(MCP_TOOLS))
def test_mcp_default_response_has_no_leak(tool, allow_pii, seeded_db):
    response = _mcp_call(tool, MCP_TOOLS[tool], allow_pii)
    assert "error" not in response
    assert not response["result"].get("isError")
    text = _decode_mcp(response)
    for leak in LEAKS:
        _assert_no_leak(leak, text)


@pytest.mark.parametrize("tool, needle", [
    ("generate_balance_sheet", ENTITY_NAME),
    ("generate_form_1065", ENTITY_NAME),
    ("generate_k1", PARTNER_NAME),
])
def test_mcp_allow_pii_opt_in_returns_names(tool, needle, seeded_db):
    """allow_pii=true is the explicit opt-in: names are present (proves the
    default-path assertions above are not vacuous)."""
    assert needle in _decode_mcp(_mcp_call(tool, MCP_TOOLS[tool], True))


def test_mcp_redaction_failure_fails_closed(seeded_db, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(privacy, "redact", _boom)
    response = _mcp_call("generate_balance_sheet", MCP_TOOLS["generate_balance_sheet"])
    assert response["error"]["code"] == -32000
    assert "result" not in response


@pytest.mark.parametrize("leak", _params(REDACT_GAPS))
def test_mcp_redact_response_free_text_has_no_leak(leak, entity_db):
    """Free-text fields (e.g. a future tool echoing descriptions) on the default path."""
    from ledger_agent.mcp.server import _redact_response

    payload = {"description": leak.text, "notes": [leak.text], "nested": {"memo": leak.text}}
    _assert_no_leak(leak, json.dumps(_redact_response(payload), ensure_ascii=False))

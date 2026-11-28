"""
tests/unit/test_cli_tax_pte.py  –  Card Q-02: every tax figure comes from ``api.pte_estimate``.

* ``ledger tax <year> --no-prompt`` prints ``api.pte_estimate(year)`` as JSON (Decimal → str)
  — in-process (``cmd_tax``) and through the real ``python -m ledger_agent.cli.main``
  entry point (R-51 four-form parity for the tax figure).
* Human ``ledger tax`` output starts with the basis line
  ``Basis: <basis> through <period> (<n>/12 months complete)`` and, when the basis is not
  full-year, the missing-statement count; it never shows the legacy individual
  SE / federal breakdown.
* Single-month views (root ``main.py tax``, the AI context export, onboarding and
  quick-scan reports) show the YEAR's ``pte_estimate`` — never one month ×12.
* The legacy ``TaxEstimator.estimate_from_net_income`` / ``estimate_from_balance_sheet``
  are gone.

Synthetic tmp_path DBs only; pseudonymous names; no real data.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FY = 2026
ENTITY_ID = "ent-q02"
ACCT_A = "acct-q02-a"
ACCT_B = "acct-q02-b"


# ── Synthetic DB ──────────────────────────────────────────────────────────────

def _account(db_file: Path, acct_id: str, entity_id: str = ENTITY_ID) -> None:
    from ledger_agent.core.database import AccountRepo
    from ledger_agent.core.models import Account, AccountType

    AccountRepo.upsert(
        Account(
            id=acct_id,
            entity_id=entity_id,
            name=f"Operating {acct_id}",
            institution="BANK_X",
            account_type=AccountType.CHECKING,
            account_number_masked="0000",
        ),
        db_file,
    )


def _month(db_file: Path, month: int, amount: str, acct_id: str = ACCT_A,
           code: str = "4020", year: int = FY) -> None:
    """One synthetic transaction plus a statement snapshot for ``year-month``."""
    from ledger_agent.core.database import SnapshotRepo, TransactionRepo
    from ledger_agent.core.models import AccountSnapshot, Transaction, TransactionType

    period = f"{year}-{month:02d}"
    amt = Decimal(amount)
    TransactionRepo.bulk_insert(
        [
            Transaction(
                id=f"{acct_id}-{period}-{code}",
                account_id=acct_id,
                date=date(year, month, 10),
                description=f"Synthetic {period}",
                amount=amt,
                transaction_type=TransactionType.CREDIT if amt > 0 else TransactionType.DEBIT,
                statement_period=period,
                coa_code=code,
                coa_name=code,
            )
        ],
        db_file,
    )
    SnapshotRepo.upsert(
        AccountSnapshot(account_id=acct_id, statement_period=period,
                        ending_balance=Decimal("10")),
        db_file,
    )


def _new_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    # Keep Schedule K hermetic: a local (gitignored) wash-sale CSV must not change
    # full-year net STCG in the in-process runs.
    import ledger_agent.core.accounting.wash_sale as wash_sale
    from ledger_agent.core.database import EntityRepo, init_db
    from ledger_agent.core.models import Entity

    monkeypatch.setattr(
        wash_sale, "disallowed_by_term", lambda *a, **kw: (Decimal("0"), Decimal("0"))
    )
    db_file = tmp_path / name
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name="ENTITY_A", entity_type="LLC", state="MO"), db_file
    )
    _account(db_file, ACCT_A)
    _account(db_file, ACCT_B)
    return db_file


@pytest.fixture
def qtd_db(tmp_path, monkeypatch) -> Path:
    """Quarter-to-date: both accounts cover Jan–Mar; only account A has April.

    Basis = quarter_to_date through FY-03 (3/12), April excluded; account B is missing
    April → 1 missing account-month.  April carries a large amount so that a
    single-month ×12 projection would be very different from the real estimate.
    """
    db_file = _new_db(tmp_path, monkeypatch, "q02_qtd.db")
    for m in (1, 2, 3):
        _month(db_file, m, "1000", ACCT_A)
        _month(db_file, m, "-100", ACCT_B, code="5010")
    _month(db_file, 4, "90000", ACCT_A)
    return db_file


@pytest.fixture
def full_db(tmp_path, monkeypatch) -> Path:
    """All 12 months for both accounts → full_year basis."""
    db_file = _new_db(tmp_path, monkeypatch, "q02_full.db")
    for m in range(1, 13):
        _month(db_file, m, "2000", ACCT_A)
        _month(db_file, m, "-500", ACCT_B, code="5010")
    return db_file


def _api_json(year: int = FY) -> dict:
    import ledger_agent.core.api as api

    return json.loads(json.dumps(dataclasses.asdict(api.pte_estimate(year)), default=str))


def _cli(capsys, *args: str) -> tuple[int, str]:
    from ledger_agent.cli.main import cmd_tax

    capsys.readouterr()
    rc = cmd_tax(list(args))
    return rc, capsys.readouterr().out


# ── 1. --no-prompt JSON parity with api.pte_estimate ─────────────────────────

@pytest.mark.parametrize("fixture", ["qtd_db", "full_db"])
def test_cli_tax_no_prompt_json_equals_api(fixture, request, capsys):
    request.getfixturevalue(fixture)
    rc, out = _cli(capsys, str(FY), "--no-prompt")
    assert rc == 0
    cli = json.loads(out)  # stdout is JSON only
    api_json = _api_json()
    assert cli == api_json
    # Non-vacuity: a real, positive PTET figure and the basis fields are present.
    assert Decimal(cli["total_annual_tax"]) > 0
    expected_basis = "quarter_to_date" if fixture == "qtd_db" else "full_year"
    assert cli["basis"] == expected_basis
    assert {"through_period", "months_covered", "missing_statements",
            "quarterly_payments", "paid_ytd", "remaining_balance"} <= set(cli)


def test_cli_tax_no_prompt_decimal_fields_are_strings(qtd_db, capsys):
    import ledger_agent.core.api as api

    _, out = _cli(capsys, str(FY), "--no-prompt")
    cli = json.loads(out)
    est = api.pte_estimate(FY)
    for name in ("total_annual_tax", "net_income", "effective_rate", "quarterly_payment",
                 "paid_ytd", "remaining_balance", "ytd_net_income", "required_to_date"):
        assert cli[name] == str(getattr(est, name)), name


def test_cli_tax_entrypoint_subprocess_parity(qtd_db):
    """The real ``ledger`` entry point (``ledger_agent.cli.main:app``) in a fresh process
    prints the same JSON as the in-process ``api.pte_estimate`` (Form A ↔ Form B)."""
    from ledger_agent.core.accounting.tax_estimator import STATE_INCOME_RATE

    env = dict(os.environ)
    env.update({
        "FI_DB_PATH": str(qtd_db),
        # Same rate the in-process estimator was imported with (a local .env never
        # overrides an explicit value).
        "FI_STATE_TAX_RATE": str(STATE_INCOME_RATE),
        "FI_AUDIT_DISABLED": "1",
        "FI_LOG_LEVEL": "ERROR",
    })
    # Run the real ``ledger`` entry function (``app``) with python-dotenv blocked, so a
    # local .env can never leak into this synthetic run.
    script = (
        "import sys; sys.modules['dotenv'] = None; "
        "from ledger_agent.cli.main import app; "
        f"sys.argv = ['ledger', 'tax', '{FY}', '--no-prompt']; app()"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout) == _api_json()


def test_cli_tax_record_payment_no_prompt_keeps_stdout_json(qtd_db, capsys):
    rc, out = _cli(capsys, str(FY), "--record-payment", "--quarter", "Q1",
                   "--amount", "25.00", "--no-prompt")
    assert rc == 0
    cli = json.loads(out)  # the confirmation went to stderr
    assert cli == _api_json()
    assert Decimal(cli["paid_ytd"]) == Decimal("25.00")


def test_cli_tax_no_data_returns_1(tmp_path, monkeypatch, capsys):
    _new_db(tmp_path, monkeypatch, "q02_empty.db")
    rc, _ = _cli(capsys, str(FY), "--no-prompt")
    assert rc == 1


# ── 2. Human output: basis line first, PTEEstimate figures, no legacy breakdown ─

def test_cli_tax_human_output_qtd(qtd_db, capsys):
    import ledger_agent.core.api as api

    est = api.pte_estimate(FY)
    rc, out = _cli(capsys, str(FY))
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0].strip() == f"Basis: quarter_to_date through {FY}-03 (3/12 months complete)"
    assert lines[1].strip().startswith("Missing statements: 1 account-month(s) across 1 account(s)")
    assert f"${est.total_annual_tax:,.2f}" in out
    assert f"{est.effective_rate}%" in out
    assert f"${est.remaining_balance:,.2f}" in out
    for q in ("Q1", "Q2", "Q3", "Q4"):
        assert any(ln.strip().startswith(f"{q} ") and "unpaid" in ln for ln in lines), q
    assert f"{FY}-04-15" in out  # Q1 due date with its year
    assert "Entity-level state PTET only" in out  # notes
    assert "Paid YTD" in out and "Remaining balance" in out
    assert "Not tax advice" in out or "not tax advice" in out.lower()
    for legacy in ("Self-Employment", "Federal Income Tax", "TOTAL ESTIMATED TAX"):
        assert legacy not in out


def test_cli_tax_human_output_full_year(full_db, capsys):
    rc, out = _cli(capsys, str(FY))
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0].strip() == f"Basis: full_year through {FY}-12 (12/12 months complete)"
    assert "Missing statements" not in out


def test_cli_tax_human_output_shows_paid_status(qtd_db, capsys):
    _cli(capsys, str(FY), "--record-payment", "--quarter", "Q1", "--amount", "1.00")
    _, out = _cli(capsys, str(FY))
    import ledger_agent.core.api as api

    paid = api.pte_estimate(FY).paid_ytd
    q1 = next(ln for ln in out.splitlines() if ln.strip().startswith("Q1 "))
    assert "partial" in q1
    assert paid > 0 and f"${paid:,.2f}" in q1


# ── 3. Single-month views show the YEAR's pte_estimate, never month ×12 ───────

def test_context_export_uses_year_pte_estimate(qtd_db):
    from adapters.context_builder import build_context, context_to_prompt

    api_json = _api_json()
    ctx = build_context(ENTITY_ID, period=f"{FY}-04")  # the big single month
    tax = ctx["tax_estimate"]
    assert tax["source"] == "api.pte_estimate"
    assert tax["basis"] == api_json["basis"] == "quarter_to_date"
    assert tax["through_period"] == f"{FY}-03"
    assert tax["months_covered"] == 3
    assert tax["missing_statement_count"] == 1 == sum(
        len(m) for m in api_json["missing_statements"].values()
    )
    assert Decimal(str(tax["total_annual_tax"])) == Decimal(api_json["total_annual_tax"])
    assert Decimal(str(tax["annual_entity_taxable_income"])) == Decimal(api_json["net_income"])
    assert [p["amount"] for p in tax["quarterly_payments"]] == [
        p["amount"] for p in api_json["quarterly_payments"]
    ]
    # Legacy individual fields are gone; no names / account ids / notes added.
    assert not {"se_tax", "federal_income_tax", "state_income_tax"} & set(tax)
    assert not {"notes", "missing_statements", "entity", "entity_name"} & set(tax)
    dumped = json.dumps(tax)
    assert ACCT_A not in dumped and ACCT_B not in dumped and "ENTITY_A" not in dumped

    prompt = context_to_prompt(ctx)
    assert f"Basis: quarter_to_date through {FY}-03 (3/12 months complete)" in prompt
    assert "SE Tax" not in prompt and "Federal Income Tax" not in prompt


def test_context_export_omits_tax_for_other_entity(qtd_db):
    """pte_estimate covers the year's most active entity; another entity's export must
    not carry those figures."""
    from adapters.context_builder import build_context, context_to_prompt
    from ledger_agent.core.database import EntityRepo
    from ledger_agent.core.models import Entity

    EntityRepo.upsert(
        Entity(id="ent-q02-other", name="ENTITY_B", entity_type="LLC", state="MO"), qtd_db
    )
    _account(qtd_db, "acct-q02-other", entity_id="ent-q02-other")
    from ledger_agent.core.database import SnapshotRepo
    from ledger_agent.core.models import AccountSnapshot

    SnapshotRepo.upsert(
        AccountSnapshot(account_id="acct-q02-other", statement_period=f"{FY}-01",
                        ending_balance=Decimal("5")),
        qtd_db,
    )
    ctx = build_context("ent-q02-other", period=f"{FY}-01")
    assert ctx["tax_estimate"] is None
    assert "Not available" in context_to_prompt(ctx)


def test_context_export_without_estimate(tmp_path, monkeypatch):
    """A year with no fully-covered month → tax_estimate None (no ×12 fallback)."""
    db_file = _new_db(tmp_path, monkeypatch, "q02_no_january.db")
    _month(db_file, 12, "100", ACCT_A, year=FY - 1)  # existed last year …
    _month(db_file, 2, "5000", ACCT_A)  # … but has no January statement this year
    from adapters.context_builder import build_context

    ctx = build_context(ENTITY_ID, period=f"{FY}-02")
    assert ctx["tax_estimate"] is None


def _import_root_main(monkeypatch):
    """Import root ``main.py`` without letting it load a local .env into the test run."""
    monkeypatch.setitem(sys.modules, "dotenv", None)
    import main as root_main

    return root_main


def test_root_main_tax_shows_year_estimate_for_a_month(qtd_db, capsys, monkeypatch):
    root_main = _import_root_main(monkeypatch)

    import ledger_agent.core.api as api

    est = api.pte_estimate(FY)
    capsys.readouterr()
    root_main._cmd_tax(f"{FY}-04")
    out = capsys.readouterr().out
    assert f"Basis: quarter_to_date through {FY}-03 (3/12 months complete)" in out
    assert f"${est.total_annual_tax:,.2f}" in out
    assert "Self-Employment" not in out


def test_onboarding_post_import_report_uses_pte_estimate(qtd_db, capsys):
    import ledger_agent.core.api as api
    from ledger_agent.cli import onboarding

    est = api.pte_estimate(FY)
    capsys.readouterr()
    onboarding._show_post_import_report()
    out = capsys.readouterr().out
    assert f"Basis: quarter_to_date through {FY}-03 (3/12 months complete)" in out
    assert f"${est.total_annual_tax:,.2f}" in out
    assert "Self-Employment" not in out


def test_quick_scan_tax_uses_pte_estimate(qtd_db, capsys):
    import ledger_agent.core.api as api
    from ledger_agent.cli import quick_scan

    est = api.pte_estimate(FY)
    capsys.readouterr()
    quick_scan._show_tax_estimate(FY)
    out = capsys.readouterr().out
    assert f"Basis: quarter_to_date through {FY}-03 (3/12 months complete)" in out
    assert f"${est.total_annual_tax:,.2f}" in out


def test_single_month_view_is_not_month_times_twelve(qtd_db, capsys):
    """April alone ×12 at the state rate would be far larger than the real estimate."""
    import ledger_agent.core.api as api
    from ledger_agent.core.accounting.tax_estimator import STATE_INCOME_RATE

    est = api.pte_estimate(FY)
    april_x12 = (Decimal("90000") * 12 * STATE_INCOME_RATE).quantize(Decimal("0.01"))
    assert est.total_annual_tax < april_x12
    from ledger_agent.cli import quick_scan

    capsys.readouterr()
    quick_scan._show_tax_estimate(FY)
    out = capsys.readouterr().out
    assert f"${april_x12:,.2f}" not in out


# ── 4. Legacy methods removed ─────────────────────────────────────────────────

def test_legacy_individual_methods_removed():
    from ledger_agent.core.accounting import tax_estimator
    from ledger_agent.core.reports import renderer

    assert not hasattr(tax_estimator.TaxEstimator, "estimate_from_net_income")
    assert not hasattr(tax_estimator.TaxEstimator, "estimate_from_balance_sheet")
    assert not hasattr(tax_estimator, "render_tax_estimate")
    assert not hasattr(renderer, "render_tax_estimate")
    # Kept: the entity-level estimator used by api.pte_estimate.
    assert hasattr(tax_estimator.TaxEstimator, "estimate_entity_ptet")
    assert callable(tax_estimator.entity_taxable_income)

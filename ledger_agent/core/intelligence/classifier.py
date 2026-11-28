"""
intelligence/classifier.py  –  Transaction → COA classifier
─────────────────────────────────────────────────────────────
Classification pipeline (in order of precedence):

  1. Already has a coa_code  →  skip (parser pre-classified it)
  2. Memory lookup (fuzzy)   →  auto-apply if score >= AUTO_CLASSIFY_THRESHOLD
                                (C-14: sign-aware; an internal-transfer rule only
                                on a row with an own-account cue)
  3. AI backend suggestion   →  use local/OpenAI/Gemini (FI_AI_BACKEND)
  4. COA keyword scan        →  auto-apply if exactly one revenue / expense COA
                                entry matches (C-11: never a balance-sheet or
                                equity account)
  5. Prompt user             →  ask, remember answer for future

The `classify_batch()` function is the primary entry point.
Pass a `prompt_fn` callable if you want interactive classification;
omit it for batch/non-interactive mode (unclassified txns get code "9999").

AI backend is selected by FI_AI_BACKEND env var:
  local  (default) – rule-based + rapidfuzz, no API key needed
  openai           – OpenAI Chat Completions (requires FI_OPENAI_API_KEY)
  gemini           – Google Gemini (requires FI_GEMINI_API_KEY)
"""
from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal

from ledger_agent.core.config import AUTO_CLASSIFY_THRESHOLD, LOCAL_CONFIDENCE_THRESHOLD
from ledger_agent.core.database import COARepo, TransactionRepo
from ledger_agent.core.intelligence import owner_draws
from ledger_agent.core.intelligence.memory import get_memory
from ledger_agent.core.logging_setup import get_logger
from ledger_agent.core.models import COAEntry, Transaction

log = get_logger(__name__)

UNCLASSIFIED_CODE = "9999"
UNCLASSIFIED_NAME = "Unclassified – Review Required"

CLASSIFIER_VERSION = "1.1"

# C-03: a bank row whose wording names a money-movement rail (ZELLE / WIRE /
# TRANSFER) but carries no own-account cue.  The parser emits it with
# ``is_transfer=False`` and this tag; the classifier codes it like any other row
# (never 9000), and ``api.import_statements`` promotes it to an internal transfer
# only when the reconciler finds the opposite leg in another account of the entity.
TRANSFER_CANDIDATE_TAG = "transfer_candidate"

# Codes a transfer candidate may not receive from generic rules, memory, AI or COA
# keywords: being internal (9000) is decided by the opposite-leg match, and the
# rail words alone are no evidence of a capital contribution (3010) or of cash (1000).
_CANDIDATE_BLOCKED_CODES = frozenset({"9000", "3010", "1000"})

# Rail words stripped from a candidate's description before it is shown to the AI
# backend, so the vendor rules can see the counterparty ("ZELLE TO ACME LAW" → LAW)
# instead of stopping at the generic ZELLE / WIRE / TRANSFER transfer rule.
_RAIL_WORDS_RE = re.compile(
    r"(?<![^\W\d_])(?:"
    r"ZELLE(?:\s*(?:BUSINESS|PAYMENT|CREDIT|DEBIT|TRANSFER))*"
    r"|WIRE(?:\s*(?:TRANSFER|INCOMING|OUTGOING|IN|OUT))*"
    r"|(?:ONLINE\s*(?:BANKING\s*)?|INTERNET\s*(?:BANKING\s*)?)?TRANSFER"
    r"(?:\s*(?:INCOMING|OUTGOING|INTO|IN|OUT))*"
    r")(?:\s*(?:TO|FROM))?(?![^\W\d_])"
)

# Transfer terms that name the entity's own counterparty account (card payoff,
# brokerage sweep): internal on their own.  The generic ones below are rails that
# also carry third-party payments: C-14 makes them internal only when the parser
# already flagged the row internal (its own-account cue), never on wording alone.
_OWN_ACCOUNT_TRANSFER_TERMS = (
    "INTERNET PAYMENT ACH TRANSF",
    "ACH CORP DEBIT TRANSFER",
    "BKG SVC LLC Z",
    "PAYMENTTO CREDIT CARD",
    "PAYMENT TO CREDIT CARD",
)
_GENERIC_TRANSFER_TERMS = (
    "TRANSFER *",
    "ZELLE BUSINESS PAYMENT",
)

# C-03: an inflow is a members' capital contribution (3010) only with an explicit
# owner / capital cue.  A bare "DEPOSIT" is usually a customer deposit, so it goes
# through the normal rules (revenue, or suspense 9999 for review).
_CAPITAL_CONTRIBUTION_RE = re.compile(
    r"(?<![^\W\d_])(?:"
    r"CAPITAL\s*CONTRIBUTION"
    r"|(?:OWNER|MEMBER|PARTNER)S?\s*(?:CAPITAL\s*)?CONTRIBUTION"
    r"|(?:OWNER|MEMBER|PARTNER)S?\s*CAPITAL(?:\s*DEPOSIT)?"
    r"|(?:OWNER|MEMBER|PARTNER)S?\s*DEPOSIT"
    r")S?(?![^\W\d_])"
)


# C-11: a "Customer Deposit" inflow (the description Bank X4 gives its Customer
# Deposits section) is service revenue.  The parser pre-codes new rows; this rule
# makes the classifier agree, so stored rows can be re-coded by re-running it.
_CUSTOMER_DEPOSIT_RE = re.compile(r"(?<![^\W\d_])CUSTOMER\s*DEPOSITS?(?![^\W\d_])")

# C-11: an outflow is an owner draw / distribution (3040) by wording only with an
# explicit owner / member / partner cue.  (Was: the 3040 COA keywords, which also
# matched a bare "DISTRIBUTION".)
_OWNER_DRAW_TEXT_RE = re.compile(
    r"(?<![^\W\d_])(?:OWNER|MEMBER|PARTNER)S?\s*(?:DRAW|DISTRIBUTION)S?(?![^\W\d_])"
)

# C-11: COA types the keyword scan may assign.  A transaction is income or an
# expense; balance-sheet accounts (assets 1xxx, liabilities 2xxx) are never its
# category ("CUSTOMER DEPOSIT" is revenue, not Cash 1000), and equity (3xxx) is
# reached only through the explicit rules above (capital contribution cue, owner
# draws, estimated-tax / withholding), never through generic keywords.
_KEYWORD_COA_TYPES = frozenset({"revenue", "expense"})


def _is_balance_sheet_code(code: str | None) -> bool:
    """C-11: asset (1xxx) or liability (2xxx) code."""
    return bool(code) and str(code)[:1] in ("1", "2")


def is_transfer_candidate(txn: Transaction) -> bool:
    return TRANSFER_CANDIDATE_TAG in (txn.tags or [])


def _has_own_account_term(desc_up: str) -> bool:
    """Wording that names the entity's own counterparty account (card payoff, sweep)."""
    return any(term in desc_up for term in _OWN_ACCOUNT_TRANSFER_TERMS)


def _has_parser_own_account_cue(txn: Transaction) -> bool:
    """C-14: the parser flagged the row internal on its own-account cue (private
    intra-transfer keywords or a card payoff).  A ``transfer_candidate`` row has no
    such cue: only the import-time opposite-leg pass can make it internal."""
    return bool(txn.is_transfer) and not is_transfer_candidate(txn)


def _strip_rail_words(description: str) -> str:
    stripped = re.sub(r"\s+", " ", _RAIL_WORDS_RE.sub(" ", description.upper())).strip()
    return stripped or description


def _keyword_match(description: str,
                   coa_entries: list[COAEntry],
                   amount: float = 0.0) -> COAEntry | None:
    desc_up = description.upper()
    matches = []
    for entry in coa_entries:
        # C-11 (generalises C-03's candidate-only rule): only revenue / expense rows
        # are eligible.  The asset, liability, equity and 9000 keyword lists contain
        # generic words ("deposit", "balance", "checking", "tax", "zelle", "wire").
        if entry.coa_type not in _KEYWORD_COA_TYPES or str(entry.code)[:1] in ("1", "2", "3"):
            continue
        # Sign guard: debits cannot be revenue; credits cannot be expenses
        if amount < 0 and entry.coa_type == "revenue":
            continue
        if amount > 0 and entry.coa_type == "expense":
            continue
        for kw in entry.keywords:
            if kw.upper() in desc_up:
                matches.append(entry)
                break
    if len(matches) == 1:
        return matches[0]
    return None


def classify_transaction(
        txn: Transaction,
        coa_entries: list[COAEntry],
        prompt_fn: Callable[[Transaction, list[COAEntry]], tuple[str, str, bool] | None] | None = None,
) -> Transaction:
    if txn.coa_code and txn.coa_code != UNCLASSIFIED_CODE:
        return txn

    memory = get_memory()

    desc_up = txn.description.upper()
    amt = float(txn.amount)

    # C-04 / D2: an outflow to an owner's own account (last-4 listed in the private
    # config) is an owner draw (3040), not a transfer. Deliberately not remembered:
    # fuzzy memory would spread the code to transfers to other accounts.
    if owner_draws.is_listed_owner_draw(txn):
        owner_draws.apply_owner_draw_rule(txn)
        return txn

    if "PAYROLL" in desc_up and amt < 0:
        if "TAX" in desc_up:
            txn.coa_code, txn.coa_name = "5040", "Payroll Tax Expense"
        else:
            txn.coa_code, txn.coa_name = "5021", "Payroll & Wages"
        memory.remember(txn.description, txn.coa_code, txn.coa_name, False, amount=amt)
        return txn

    # Modern payroll providers — bank descriptions typically include the
    # provider name but not the literal word "PAYROLL". Extends the rule
    # above so those outflows still route to L9 salaries / L18 payroll tax.
    _payroll_providers = (
        "ADP",               # ADP payroll
        "GUSTO",             # Gusto payroll
        "PAYCHEX",           # Paychex
        "JUSTWORKS",         # Justworks PEO
        "RIPPLING",          # Rippling
        "TRINET",            # TriNet
        "INTUIT PAYROLL",
        "QUICKBOOKS PAYROLL",
        "SQUARE PAYROLL",
        "WAVE PAYROLL",
    )
    if amt < 0 and any(p in desc_up for p in _payroll_providers):
        if "TAX" in desc_up or "WITHHOLDING" in desc_up:
            txn.coa_code, txn.coa_name = "5040", "Payroll Tax Expense"
        else:
            txn.coa_code, txn.coa_name = "5021", "Payroll & Wages"
        memory.remember(txn.description, txn.coa_code, txn.coa_name, False, amount=amt)
        return txn

    if "USATAXPYMT" in desc_up and amt < 0:
        txn.coa_code, txn.coa_name = "5040", "Payroll Tax Expense"
        memory.remember(txn.description, txn.coa_code, txn.coa_name, False, amount=amt)
        return txn

    estimated_tax_terms = (
        "MO-PTE",
        "MO PTE",
        "ESTIMATED TAX",
        "QUARTERLY TAX",
        "IRS TREAS",
        "FEDERAL TAX",
        "STATE TAX",
        "DEPT OF REV",
        "DEPT OF REVENUE",
        "MO DOR",
    )
    if amt < 0 and any(term in desc_up for term in estimated_tax_terms):
        txn.coa_code, txn.coa_name = "3040", "Members Distributions / Owner Draws"
        memory.remember(txn.description, txn.coa_code, txn.coa_name, False, amount=amt)
        return txn

    candidate = is_transfer_candidate(txn)
    # C-14: an own-account cue is own-account wording or the parser's own flag.
    # Generic rail wording ("TRANSFER *", "ZELLE BUSINESS PAYMENT") is not one.
    own_term = _has_own_account_term(desc_up)
    parser_cue = _has_parser_own_account_cue(txn)

    if own_term or (
        parser_cue and any(term in desc_up for term in _GENERIC_TRANSFER_TERMS)
    ):
        txn.coa_code, txn.coa_name = "9000", "Inter-Account Transfer"
        txn.is_transfer = True
        if not candidate:
            memory.remember(txn.description, txn.coa_code, txn.coa_name, True, amount=amt)
        return txn

    # C-03: capital contribution only with an owner / capital cue (was: any bare
    # "DEPOSIT", which booked ordinary customer deposits as capital).
    if amt > 0 and _CAPITAL_CONTRIBUTION_RE.search(desc_up):
        txn.coa_code, txn.coa_name = "3010", "Members Capital Contributions"
        return txn

    # C-11: customer deposits are revenue (was: "deposit" keyword → Cash 1000).
    if amt > 0 and _CUSTOMER_DEPOSIT_RE.search(desc_up):
        txn.coa_code, txn.coa_name = "4020", "Service Revenue"
        return txn

    # C-03: a transfer candidate never takes 9000 / 3010 / 1000 from memory, AI or
    # keywords, and its result is not remembered (fuzzy memory would spread a
    # third-party payee's code to other Zelle / wire rows).
    # C-14: the lookup is sign-aware (revenue rules only on inflows, expense rules
    # only on outflows) and replays an internal-transfer rule only on a row with an
    # own-account cue; a learned 9000 is otherwise wording-only.
    result = memory.lookup(
        txn.description, amt, own_account_cue=(own_term or parser_cue) and not candidate
    )
    if result:
        code, name, is_xfer, score = result
        # C-11: a remembered balance-sheet code was learned from the old keyword
        # scan ("deposit" → Cash 1000); do not replay it.
        if score >= AUTO_CLASSIFY_THRESHOLD and not _is_balance_sheet_code(code) and not (
            candidate and (is_xfer or code in _CANDIDATE_BLOCKED_CODES)
        ):
            txn.coa_code = code
            txn.coa_name = name
            txn.is_transfer = is_xfer
            return txn

    try:
        from ledger_agent.core.intelligence.ai_backend import get_backend
        backend = get_backend()
        ai_result = backend.classify_transaction(
            description=_strip_rail_words(txn.description) if candidate else txn.description,
            amount=float(txn.amount),
        )
        if ai_result and ai_result.get("confidence", 0) >= LOCAL_CONFIDENCE_THRESHOLD:
            code = ai_result.get("coa_code", "")
            name = ai_result.get("coa_name", "")
            xfer = bool(ai_result.get("is_transfer", False))
            if candidate and (xfer or code in _CANDIDATE_BLOCKED_CODES):
                code = ""  # the opposite-leg match decides; fall through
            if _is_balance_sheet_code(code):
                code = ""  # C-11: a transaction is never an asset / liability account
            if code and code != UNCLASSIFIED_CODE:
                txn.coa_code = code
                txn.coa_name = name
                txn.is_transfer = xfer
                if not candidate:
                    memory.remember(txn.description, code, name, xfer, amount=amt)
                log.debug(
                    "AI classified transaction",
                    extra={
                        "backend": backend.backend_name,
                        "code": code,
                        "confidence": ai_result.get("confidence"),
                    },
                )
                return txn
    except Exception as exc:
        log.debug("AI backend skipped", extra={"error": str(exc)})

    # C-11: explicit owner-draw wording (replaces the 3040 keyword list).
    if amt < 0 and _OWNER_DRAW_TEXT_RE.search(desc_up):
        txn.coa_code, txn.coa_name = "3040", "Members Distributions / Owner Draws"
        return txn

    kw_match = _keyword_match(txn.description, coa_entries, amt)
    if kw_match:
        txn.coa_code = kw_match.code
        txn.coa_name = kw_match.name
        if not candidate:
            memory.remember(txn.description, kw_match.code, kw_match.name, amount=amt)
        return txn

    if prompt_fn:
        user_result = prompt_fn(txn, coa_entries)
        if user_result:
            code, name, is_xfer = user_result
            txn.coa_code = code
            txn.coa_name = name
            txn.is_transfer = is_xfer
            memory.remember(txn.description, code, name, is_xfer, amount=amt)
            try:
                from ledger_agent.core.intelligence.ai_backend import get_backend
                get_backend().on_user_confirmed(txn.description, code, name, is_xfer)
            except Exception:
                pass
            return txn

    txn.coa_code = UNCLASSIFIED_CODE
    txn.coa_name = UNCLASSIFIED_NAME
    return txn


def classify_batch(
        transactions: list[Transaction],
        prompt_fn: Callable | None = None,
        confidence: float = 0.0,
) -> tuple[list[Transaction], int, int]:
    try:
        from ledger_agent.core.audit import audit as _audit
    except Exception:
        _audit = None  # type: ignore

    coa_entries = COARepo.list_all()
    auto = prompted = 0

    for txn in transactions:
        prior_code = txn.coa_code or ""
        txn = classify_transaction(txn, coa_entries, prompt_fn)
        new_code = txn.coa_code or ""
        # C-04 / D2: tag outgoing transfers to unlisted outside accounts
        # (owner_draw_candidate); listed owner draws were recoded above.
        owner_rule = owner_draws.apply_owner_draw_rule(txn) if new_code else None

        if new_code and new_code != UNCLASSIFIED_CODE:
            if new_code != prior_code:
                if prompt_fn and not prior_code:
                    prompted += 1
                else:
                    auto += 1

        # R-65 / ARCH-27: persist with classifier metadata
        if txn.id and new_code:
            if owner_rule == "draw":
                xfer_flag: bool | None = False  # a draw leaves the entity: clear the flag
            else:
                xfer_flag = True if txn.is_transfer else None  # add-only otherwise
            TransactionRepo.update_coa_with_meta(
                txn.id, txn.coa_code, txn.coa_name,
                CLASSIFIER_VERSION, confidence,
                is_transfer=xfer_flag,
            )
            if owner_rule:
                _persist_owner_draw_fields(txn)

            # R-66: emit classifier.assigned or classifier.reassigned
            if _audit:
                if prior_code and prior_code != new_code:
                    _audit(
                        "classifier.reassigned",
                        transaction_id=txn.id,
                        prior_coa_code=prior_code,
                        coa_code=new_code,
                        classifier_version=CLASSIFIER_VERSION,
                        confidence=confidence,
                    )
                elif not prior_code:
                    _audit(
                        "classifier.assigned",
                        transaction_id=txn.id,
                        coa_code=new_code,
                        classifier_version=CLASSIFIER_VERSION,
                        confidence=confidence,
                    )

    return transactions, auto, prompted


def _persist_owner_draw_fields(txn: Transaction) -> None:
    """Persist the tags / transaction type set by the owner-draw rule (C-04).

    ``update_coa_with_meta`` stores neither, and the rows were inserted before
    classification ran.
    """
    import json

    from ledger_agent.core.database import get_conn

    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET tags=?, transaction_type=? WHERE id=?",
            (json.dumps(txn.tags), txn.transaction_type.value, txn.id),
        )


def coa_choices_for_prompt(coa_entries: list[COAEntry]) -> list[tuple[str, str]]:
    leaves = [e for e in coa_entries if e.parent_code is not None]
    return [(f"{e.code}  {e.name}", e.code) for e in leaves]


def suggest_classification(description: str, amount: float = 0.0) -> dict:
    memory = get_memory()
    # C-14: sign-aware; a remembered transfer only with own-account wording.
    result = memory.lookup(
        description, amount, own_account_cue=_has_own_account_term(description.upper())
    )
    if result and not _is_balance_sheet_code(result[0]):  # C-11
        code, name, _, score = result
        return {
            "coa_code": code,
            "coa_name": name,
            "confidence": round(score / 100, 2),
            "source": "memory",
        }
    try:
        from ledger_agent.core.intelligence.ai_backend import get_backend
        backend = get_backend()
        ai_result = backend.classify_transaction(description=description, amount=amount)
        if ai_result and ai_result.get("coa_code") and not _is_balance_sheet_code(
            ai_result.get("coa_code")
        ):
            return {
                "coa_code": ai_result.get("coa_code", ""),
                "coa_name": ai_result.get("coa_name", ""),
                "confidence": ai_result.get("confidence", 0.0),
                "source": backend.backend_name,
            }
    except Exception:
        pass
    coa_entries = COARepo.list_all()
    kw_match = _keyword_match(description, coa_entries, amount)
    if kw_match:
        return {
            "coa_code": kw_match.code,
            "coa_name": kw_match.name,
            "confidence": 0.7,
            "source": "keyword",
        }
    return {
        "coa_code": UNCLASSIFIED_CODE,
        "coa_name": UNCLASSIFIED_NAME,
        "confidence": 0.0,
        "source": "none",
    }


def summarise_classifications(transactions: list[Transaction]) -> dict:
    summary: dict = {}
    for t in transactions:
        code = t.coa_code or UNCLASSIFIED_CODE
        if code not in summary:
            summary[code] = {
                "name": t.coa_name or UNCLASSIFIED_NAME,
                "total": Decimal("0"),
                "count": 0,
            }
        summary[code]["total"] += t.amount
        summary[code]["count"] += 1
    return summary

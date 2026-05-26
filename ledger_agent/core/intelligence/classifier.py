"""
intelligence/classifier.py  –  Transaction → COA classifier
─────────────────────────────────────────────────────────────
Classification pipeline (in order of precedence):

  1. Already has a coa_code  →  skip (parser pre-classified it)
  2. Memory lookup (fuzzy)   →  auto-apply if score >= AUTO_CLASSIFY_THRESHOLD
  3. AI backend suggestion   →  use local/OpenAI/Gemini (FI_AI_BACKEND)
  4. COA keyword scan        →  auto-apply if exactly one COA entry matches
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

from decimal import Decimal
from typing import Callable, List, Optional, Tuple

from config import AUTO_CLASSIFY_THRESHOLD, LOCAL_CONFIDENCE_THRESHOLD
from ledger_agent.core.database import COARepo, TransactionRepo
from ledger_agent.core.logging_setup import get_logger
from ledger_agent.core.models import COAEntry, Transaction
from ledger_agent.core.intelligence.memory import get_memory

log = get_logger(__name__)

UNCLASSIFIED_CODE = "9999"
UNCLASSIFIED_NAME = "Unclassified – Review Required"

CLASSIFIER_VERSION = "1.1"


def _keyword_match(description: str,
                   coa_entries: List[COAEntry],
                   amount: float = 0.0) -> Optional[COAEntry]:
    desc_up = description.upper()
    matches = []
    for entry in coa_entries:
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
        coa_entries: List[COAEntry],
        prompt_fn: Optional[Callable[[Transaction, List[COAEntry]], Optional[Tuple[str, str, bool]]]] = None,
) -> Transaction:
    if txn.coa_code and txn.coa_code != UNCLASSIFIED_CODE:
        return txn

    memory = get_memory()

    desc_up = txn.description.upper()
    amt = float(txn.amount)

    if "PAYROLL" in desc_up and amt < 0:
        if "TAX" in desc_up:
            txn.coa_code, txn.coa_name = "5040", "Payroll Tax Expense"
        else:
            txn.coa_code, txn.coa_name = "5021", "Payroll & Wages"
        memory.remember(txn.description, txn.coa_code, txn.coa_name, False)
        return txn

    if "USPSPO" in desc_up and amt < -500:
        txn.coa_code, txn.coa_name = "3040", "Members Distributions / Owner Draws"
        memory.remember(txn.description, txn.coa_code, txn.coa_name, False)
        return txn

    if "USATAXPYMT" in desc_up and amt < 0:
        txn.coa_code, txn.coa_name = "5040", "Payroll Tax Expense"
        memory.remember(txn.description, txn.coa_code, txn.coa_name, False)
        return txn

    result = memory.lookup(txn.description)
    if result:
        code, name, is_xfer, score = result
        if score >= AUTO_CLASSIFY_THRESHOLD:
            txn.coa_code = code
            txn.coa_name = name
            txn.is_transfer = is_xfer
            return txn

    try:
        from ledger_agent.core.intelligence.ai_backend import get_backend
        backend = get_backend()
        ai_result = backend.classify_transaction(
            description=txn.description,
            amount=float(txn.amount),
        )
        if ai_result and ai_result.get("confidence", 0) >= LOCAL_CONFIDENCE_THRESHOLD:
            code = ai_result.get("coa_code", "")
            name = ai_result.get("coa_name", "")
            xfer = bool(ai_result.get("is_transfer", False))
            if code and code != UNCLASSIFIED_CODE:
                txn.coa_code = code
                txn.coa_name = name
                txn.is_transfer = xfer
                memory.remember(txn.description, code, name, xfer)
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

    kw_match = _keyword_match(txn.description, coa_entries, amt)
    if kw_match:
        txn.coa_code = kw_match.code
        txn.coa_name = kw_match.name
        memory.remember(txn.description, kw_match.code, kw_match.name)
        return txn

    if prompt_fn:
        user_result = prompt_fn(txn, coa_entries)
        if user_result:
            code, name, is_xfer = user_result
            txn.coa_code = code
            txn.coa_name = name
            txn.is_transfer = is_xfer
            memory.remember(txn.description, code, name, is_xfer)
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
        transactions: List[Transaction],
        prompt_fn: Optional[Callable] = None,
        confidence: float = 0.0,
) -> Tuple[List[Transaction], int, int]:
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

        if new_code and new_code != UNCLASSIFIED_CODE:
            if new_code != prior_code:
                if prompt_fn and not prior_code:
                    prompted += 1
                else:
                    auto += 1

        # R-65 / ARCH-27: persist with classifier metadata
        if txn.id and new_code:
            TransactionRepo.update_coa_with_meta(
                txn.id, txn.coa_code, txn.coa_name,
                CLASSIFIER_VERSION, confidence,
            )

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


def coa_choices_for_prompt(coa_entries: List[COAEntry]) -> List[Tuple[str, str]]:
    leaves = [e for e in coa_entries if e.parent_code is not None]
    return [(f"{e.code}  {e.name}", e.code) for e in leaves]


def suggest_classification(description: str, amount: float = 0.0) -> dict:
    memory = get_memory()
    result = memory.lookup(description)
    if result:
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
        if ai_result and ai_result.get("coa_code"):
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


def summarise_classifications(transactions: List[Transaction]) -> dict:
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

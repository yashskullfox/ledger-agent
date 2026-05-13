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

from config import AUTO_CLASSIFY_THRESHOLD
from core.database import COARepo, TransactionRepo
from core.logging_setup import get_logger
from core.models import COAEntry, Transaction
from intelligence.memory import get_memory

log = get_logger(__name__)

# Sentinel for "user skipped classification"
UNCLASSIFIED_CODE = "9999"
UNCLASSIFIED_NAME = "Unclassified – Review Required"

def _keyword_match(description: str,
                   coa_entries: List[COAEntry]) -> Optional[COAEntry]:
    """
    Return the single best COA entry whose keywords appear in `description`.
    If multiple entries match, return None (ambiguous → prompt user).
    """
    desc_up = description.upper()
    matches = []
    for entry in coa_entries:
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
    """
    Classify a single transaction in-place (mutates coa_code / coa_name).
    Returns the (possibly updated) transaction.
    """
    # 1. Already classified by parser
    if txn.coa_code and txn.coa_code != UNCLASSIFIED_CODE:
        return txn

    memory = get_memory()

    # 2. Memory lookup
    result = memory.lookup(txn.description)
    if result:
        code, name, is_xfer, score = result
        if score >= AUTO_CLASSIFY_THRESHOLD:
            txn.coa_code    = code
            txn.coa_name    = name
            txn.is_transfer = is_xfer
            return txn
        # Medium confidence: note it but still check keyword scan

    # 3. AI backend suggestion
    try:
        from intelligence.ai_backend import get_backend
        backend = get_backend()
        ai_result = backend.classify_transaction(
            description=txn.description,
            amount=float(txn.amount),
        )
        if ai_result and ai_result.get("confidence", 0) >= 0.75:
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

    # 4. COA keyword scan
    kw_match = _keyword_match(txn.description, coa_entries)
    if kw_match:
        txn.coa_code = kw_match.code
        txn.coa_name = kw_match.name
        memory.remember(txn.description, kw_match.code, kw_match.name)
        return txn

    # 5. Interactive prompt
    if prompt_fn:
        user_result = prompt_fn(txn, coa_entries)
        if user_result:
            code, name, is_xfer = user_result
            txn.coa_code    = code
            txn.coa_name    = name
            txn.is_transfer = is_xfer
            memory.remember(txn.description, code, name, is_xfer)
            # Signal the AI backend to learn from this confirmation
            try:
                from intelligence.ai_backend import get_backend
                get_backend().on_user_confirmed(txn.description, code, name, is_xfer)
            except Exception:
                pass
            return txn

    # 6. Fallback: unclassified
    txn.coa_code = UNCLASSIFIED_CODE
    txn.coa_name = UNCLASSIFIED_NAME
    return txn

def classify_batch(
    transactions: List[Transaction],
    prompt_fn: Optional[Callable] = None,
) -> Tuple[List[Transaction], int, int]:
    """
    Classify a list of transactions.

    Returns:
        (classified_txns, auto_count, prompted_count)
    """
    coa_entries = COARepo.list_all()
    auto = prompted = 0

    for txn in transactions:
        before = txn.coa_code
        txn = classify_transaction(txn, coa_entries, prompt_fn)
        if txn.coa_code and txn.coa_code != UNCLASSIFIED_CODE:
            if txn.coa_code != before:
                if prompt_fn and before in ("", None):
                    prompted += 1
                else:
                    auto += 1
        # Persist the classification
        if txn.id and txn.coa_code:
            TransactionRepo.update_coa(txn.id, txn.coa_code, txn.coa_name)

    return transactions, auto, prompted

def coa_choices_for_prompt(coa_entries: List[COAEntry]) -> List[Tuple[str, str]]:
    """
    Return a list of (display_label, code) for presenting to the user.
    Leaf entries only (those with a parent_code) – easier to navigate.
    """
    leaves = [e for e in coa_entries if e.parent_code is not None]
    return [(f"{e.code}  {e.name}", e.code) for e in leaves]

def suggest_classification(description: str, amount: float = 0.0) -> dict:
    """
    Return a classification suggestion dict for a single description string.
    Used by the MCP server and any non-interactive caller that needs a COA suggestion.
    Returns {"coa_code": str, "coa_name": str, "confidence": float, "source": str}
    """
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
        from intelligence.ai_backend import get_backend
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
    kw_match = _keyword_match(description, coa_entries)
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
    """
    Return {coa_code: {"name": ..., "total": Decimal, "count": int}}
    """
    summary: dict = {}
    for t in transactions:
        code = t.coa_code or UNCLASSIFIED_CODE
        if code not in summary:
            summary[code] = {
                "name":  t.coa_name or UNCLASSIFIED_NAME,
                "total": Decimal("0"),
                "count": 0,
            }
        summary[code]["total"] += t.amount
        summary[code]["count"] += 1
    return summary

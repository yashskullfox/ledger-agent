"""
core/privacy.py  –  Enterprise PII / sensitive-data egress firewall (R-46)
───────────────────────────────────────────────────────────────────────────
Single chokepoint for all outbound text.  Tokenises PII before any remote
AI call and un-redacts on the return path.  The RedactionMap is NEVER
serialised, never written to disk, never logged.

Usage:
    from core.privacy import redact, unredact, unredact_result, audit_egress

    # Before remote call
    safe_desc, m1 = redact(description, scope="openai")
    safe_entity, m2 = redact(entity_name, scope="openai")
    raw_response = remote_call(safe_desc, safe_entity)
    # After remote call — local display only
    display_reason = unredact(raw_response, {**m1, **m2})

Scopes:
    openai | gemini   – all 12 detector categories
    ai_context        – all 12 detector categories
    mcp_response      – all 12 detector categories
    memory_file       – structural PII + entity/person names
    log               – SSN, EIN, API keys only (high-precision, low-noise)

Environment variables (read via config.py):
    FI_AI_EGRESS_MODE      redact | strict | mock | passthrough  (default: redact)
    FI_AI_EGRESS_MODE_ACK  I_understand_the_risk  (required for passthrough)
    FI_PRIVACY_ENTITY_NAME Legal entity name to token-replace as <ENTITY_NAME>
    FI_PRIVACY_NER         spacy  (activates spaCy NER; optional dependency)
"""
from __future__ import annotations

import json
import logging as _logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ── Public types ─────────────────────────────────────────────────────────────

RedactionMap = Dict[str, str]
"""Maps redaction-token → original value.  NEVER serialise or persist this."""


class PrivacyLeakError(RuntimeError):
    """
    Raised by audit_egress() when PII is detected in an outbound payload
    that was supposed to have already been redacted.  Blocks the HTTP call.
    Also raised by redact() when an API key is found in any outbound payload.
    """


# ── Luhn checksum ─────────────────────────────────────────────────────────────

def _luhn_valid(n: str) -> bool:
    """Return True if n (digits-only string) passes the Luhn algorithm."""
    digits = [int(c) for c in n]
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ── ABA routing checksum ──────────────────────────────────────────────────────

def _aba_valid(n: str) -> bool:
    """Return True if n (9-digit string) passes the ABA routing checksum."""
    if len(n) != 9 or not n.isdigit():
        return False
    weights = [3, 7, 1, 3, 7, 1, 3, 7, 1]
    total = sum(int(d) * w for d, w in zip(n, weights))
    return total % 10 == 0


# ── Vendor allowlist (loaded once) ────────────────────────────────────────────

_ALLOWLIST: Optional[Set[str]] = None


def _load_allowlist() -> Set[str]:
    global _ALLOWLIST
    if _ALLOWLIST is not None:
        return _ALLOWLIST
    allowlist_path = Path(__file__).parent / "privacy_allowlist.txt"
    if allowlist_path.exists():
        entries: Set[str] = set()
        for line in allowlist_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                entries.add(stripped.upper())
        _ALLOWLIST = entries
    else:
        _ALLOWLIST = set()
    return _ALLOWLIST


# ── Banking/system words — never redact as counterparties ─────────────────────

_SYSTEM_WORDS: Set[str] = {
    "ACH", "ATM", "LLC", "INC", "CORP", "LTD", "DBA", "FBO",
    "REF", "CHK", "DEP", "PMT", "PAY", "TXN", "TRN", "EFT",
    "PPD", "CCD", "WEB", "TEL", "CTX", "IAT", "POS",
    "TRANSFER", "PAYMENT", "DEPOSIT", "WITHDRAWAL", "PURCHASE",
    "DEBIT", "CREDIT", "CHARGE", "REFUND", "FEE", "INTEREST",
    "BALANCE", "AVAILABLE", "PENDING", "CLEARED", "POSTED",
    "USD", "WIRE", "SWIFT", "IBAN", "BIC",
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
}


# ── API key patterns ──────────────────────────────────────────────────────────

_API_KEY_PATTERNS: List[re.Pattern] = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),             # OpenAI
    re.compile(r"AIza[A-Za-z0-9_\-]{35}"),          # Google/Gemini
    re.compile(r"xoxb-[A-Za-z0-9\-]{50,}"),         # Slack
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),            # GitHub PAT
]

# ── SSN ───────────────────────────────────────────────────────────────────────

_SSN_HYPH = re.compile(r"\b(\d{3})-(\d{2})-(\d{4})\b")
_SSN_BARE_CTX = re.compile(r"\b(?:SSN|TIN|TAX[\s\-]?ID|SOCIAL[\s\-]?SECURITY)\b", re.IGNORECASE)

# ── EIN ───────────────────────────────────────────────────────────────────────

_EIN_PAT = re.compile(r"\b(\d{2})-(\d{7})\b")

# ── Credit card PAN ───────────────────────────────────────────────────────────

_CARD_CANDIDATE = re.compile(r"(?<!\d)(\d[ \-]?){13,19}(?!\d)")

# ── ABA routing ───────────────────────────────────────────────────────────────

_ROUTING_CTX = re.compile(r"\b(?:ROUTING|RTN|ABA|TRANSIT)\b", re.IGNORECASE)
_NINE_DIGITS = re.compile(r"(?<!\d)(\d{9})(?!\d)")

# ── Bank account ──────────────────────────────────────────────────────────────

_ACCT_CTX = re.compile(
    r"\b(?:ACCOUNT|ACCT|A/C|A\.C\.|CHECKING|SAVINGS|DEPOSIT\s+ACCT)\b",
    re.IGNORECASE,
)
_ACCT_DIGITS = re.compile(r"(?<!\d)(\d{8,17})(?!\d)")

# ── Email ─────────────────────────────────────────────────────────────────────

_EMAIL_PAT = re.compile(
    r"\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,8}\b"
)

# ── Phone (NANP) ──────────────────────────────────────────────────────────────

_PHONE_PAT = re.compile(
    r"(?<!\d)"
    r"(?:(?:\+1[\s\-.]?)?)?"
    r"(?:\(?\d{3}\)?[\s\-.]?)"
    r"\d{3}[\s\-.]?"
    r"\d{4}"
    r"(?!\d)"
)

# ── Street address ────────────────────────────────────────────────────────────

_ADDR_PAT = re.compile(
    r"\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+"
    r"(?:St(?:reet)?|Ave(?:nue)?|Rd|Road|Blvd|Boulevard|Dr(?:ive)?|"
    r"Ln|Lane|Ct|Court|Way|Pl(?:ace)?|Pkwy|Hwy|Highway)\b",
    re.IGNORECASE,
)

# ── Person name (context-gated heuristic) ─────────────────────────────────────
# Context keywords are case-insensitive via inline (?i:...) group.
# The name capture group is intentionally case-sensitive so "LLC" / "CORP"
# (all-caps) are excluded — proper names have lowercase letters after the initial.

_PERSON_CTX_PAT = re.compile(
    r"(?i:\b(?:PAYEE|BENEFICIARY|BENE|PAY\s+TO(?:\s+THE\s+ORDER\s+OF)?|"
    r"PAID\s+TO|PAYMENT\s+TO|TRANSFER\s+TO|SENT\s+TO|FROM)\s*:?\s+)"
    r"([A-Z][a-z]{1,20}(?:\s+[A-Z]\.?)?"               # first [middle init]
    r"(?:\s+[A-Z][a-z]{1,20}){1,2})",                   # last [optional suffix]
)

# ── ALL-CAPS counterparty business name ───────────────────────────────────────

_ALLCAPS_SEQ_PAT = re.compile(r"\b([A-Z]{2,}(?:[\s&][A-Z]{2,})+)\b")


# ── Per-session pseudonym registry ────────────────────────────────────────────
# Maps (category, original_value) → token.
# NEVER persisted; regenerated each process invocation.

_session_map: Dict[Tuple[str, str], str] = {}
_session_counters: Dict[str, int] = {}


def _reset_session() -> None:
    """Reset per-session pseudonym state.  Call from tests between cases."""
    _session_map.clear()
    _session_counters.clear()


def _stable_token(category: str, original: str, hint: str = "") -> str:
    """
    Return a stable-per-session token for (category, original).
    hint: optional suffix embedded in the token (e.g. last4 of card/account).
    Within one process invocation, the same (category, original) always
    produces the same token — stable pseudonyms for coherent AI reasoning.
    """
    key = (category, original)
    if key not in _session_map:
        n = _session_counters.get(category, 0) + 1
        _session_counters[category] = n
        if hint:
            token = f"<{category}_{hint}>"
        else:
            token = f"<{category}_{n:03d}>"
        _session_map[key] = token
    return _session_map[key]


# ── Individual detector functions ─────────────────────────────────────────────

def _detect_api_keys(text: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for pat in _API_KEY_PATTERNS:
        for m in pat.finditer(text):
            hits.append((m.group(0), "<SECRET_***>"))
    return hits


def _detect_ssn(text: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    # Always detect hyphenated SSN
    for m in _SSN_HYPH.finditer(text):
        orig = m.group(0)
        if orig not in seen:
            seen.add(orig)
            hits.append((orig, "<SSN_***>"))

    # Bare 9-digit only with SSN context word within 40 chars
    for m in _NINE_DIGITS.finditer(text):
        orig = m.group(0)
        if orig in seen:
            continue
        start = max(0, m.start() - 40)
        end = min(len(text), m.end() + 40)
        window = text[start:end]
        if _SSN_BARE_CTX.search(window) and not _aba_valid(orig):
            seen.add(orig)
            hits.append((orig, "<SSN_***>"))

    return hits


def _detect_ein(text: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for m in _EIN_PAT.finditer(text):
        hits.append((m.group(0), "<EIN_***>"))
    return hits


def _detect_credit_cards(text: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for m in _CARD_CANDIDATE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"[ \-]", "", raw)
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            last4 = digits[-4:]
            token = _stable_token("CARD", digits, f"xxx{last4}")
            hits.append((raw, token))
    return hits


def _detect_routing(text: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for m in _NINE_DIGITS.finditer(text):
        orig = m.group(0)
        if not _aba_valid(orig):
            continue
        start = max(0, m.start() - 40)
        end = min(len(text), m.end() + 40)
        window = text[start:end]
        if _ROUTING_CTX.search(window):
            hits.append((orig, "<ROUTING_***>"))
    return hits


def _detect_account_numbers(text: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for m in _ACCT_DIGITS.finditer(text):
        orig = m.group(0)
        start = max(0, m.start() - 40)
        end = min(len(text), m.end() + 40)
        window = text[start:end]
        if _ACCT_CTX.search(window):
            last4 = orig[-4:]
            token = _stable_token("ACCT", orig, f"xxx{last4}")
            hits.append((orig, token))
    return hits


def _detect_emails(text: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for m in _EMAIL_PAT.finditer(text):
        orig = m.group(0)
        token = _stable_token("EMAIL", orig)
        hits.append((orig, token))
    return hits


def _detect_phones(text: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for m in _PHONE_PAT.finditer(text):
        orig = m.group(0)
        digits = re.sub(r"\D", "", orig)
        if len(digits) < 10:
            continue
        token = _stable_token("PHONE", orig)
        hits.append((orig, token))
    return hits


def _detect_addresses(text: str) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    for m in _ADDR_PAT.finditer(text):
        orig = m.group(0)
        token = _stable_token("ADDR", orig)
        hits.append((orig, token))
    return hits


def _detect_person_names(text: str) -> List[Tuple[str, str]]:
    allowlist = _load_allowlist()
    hits: List[Tuple[str, str]] = []
    for m in _PERSON_CTX_PAT.finditer(text):
        # Group 1 is the name (the (?i:...) context part is a non-capturing group)
        name = m.group(1).strip()
        if name.upper() in allowlist:
            continue
        token = _stable_token("PERSON", name)
        hits.append((name, token))
    return hits


# ── Company-name suffixes (these alone don't disqualify a counterparty match) ─

_COMPANY_SUFFIXES: Set[str] = {
    "LLC", "INC", "CORP", "LTD", "DBA", "CO", "LP", "LLP", "PC", "PA",
    "PLLC", "PLC", "NV", "SA", "AG", "GMBH",
}


def _detect_counterparty_names(text: str) -> List[Tuple[str, str]]:
    allowlist = _load_allowlist()
    hits: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for m in _ALLCAPS_SEQ_PAT.finditer(text):
        orig = m.group(0).strip()
        if orig in seen or len(orig) < 5:
            continue

        words = re.split(r"[\s&]+", orig)

        # Identify the "meaningful" words (non-suffix words)
        meaningful = [w for w in words if w not in _COMPANY_SUFFIXES]

        # Skip if there are no meaningful words
        if not meaningful:
            continue

        # Skip if all meaningful words are system words
        if all(w in _SYSTEM_WORDS for w in meaningful):
            continue

        # Skip if any meaningful word is in the vendor allowlist
        if any(w in allowlist for w in meaningful):
            continue

        # Skip if the entire sequence is in the allowlist
        if orig in allowlist:
            continue

        seen.add(orig)
        token = _stable_token("COUNTERPARTY", orig)
        hits.append((orig, token))
    return hits


def _detect_entity_name(text: str, entity_name: str) -> List[Tuple[str, str]]:
    if not entity_name:
        return []
    pattern = re.compile(re.escape(entity_name), re.IGNORECASE)
    hits: List[Tuple[str, str]] = []
    for m in pattern.finditer(text):
        hits.append((m.group(0), "<ENTITY_NAME>"))
    return hits


# ── Scope → active detector set ──────────────────────────────────────────────

_ALL_DETECTORS = [
    "api_key", "ssn", "ein", "credit_card", "routing", "account",
    "email", "phone", "address", "entity_name", "person_name", "counterparty",
]

_SCOPE_DETECTORS: Dict[str, List[str]] = {
    "openai":       _ALL_DETECTORS,
    "gemini":       _ALL_DETECTORS,
    "ai_context":   _ALL_DETECTORS,
    "mcp_response": _ALL_DETECTORS,
    "memory_file":  [
        "api_key", "ssn", "ein", "credit_card", "routing",
        "account", "email", "phone", "entity_name", "person_name",
    ],
    "log": ["api_key", "ssn", "ein"],
}


# ── Main redact() function ────────────────────────────────────────────────────

def redact(
    text: str,
    scope: str = "openai",
    entity_name: str = "",
) -> Tuple[str, RedactionMap]:
    """
    Tokenise PII in `text` before sending to a remote service.

    Returns (safe_text, RedactionMap).
    The RedactionMap maps token → original for the unredact() return path.
    It is NEVER serialised or written to disk.

    Raises PrivacyLeakError if API keys are found (should never occur —
    defence-in-depth above config.py's build-time secret guard).
    """
    from config import AI_EGRESS_MODE, AI_EGRESS_MODE_ACK, PRIVACY_ENTITY_NAME

    eff_entity = entity_name or PRIVACY_ENTITY_NAME

    # ── passthrough mode ──────────────────────────────────────────────────────
    if AI_EGRESS_MODE == "passthrough":
        if AI_EGRESS_MODE_ACK != "I_understand_the_risk":
            raise RuntimeError(
                "FI_AI_EGRESS_MODE=passthrough requires "
                "FI_AI_EGRESS_MODE_ACK=I_understand_the_risk to be set."
            )
        _log_egress_event(scope, 0, "passthrough")
        return text, {}

    # ── select detectors for this scope ──────────────────────────────────────
    detectors = _SCOPE_DETECTORS.get(scope, _ALL_DETECTORS)

    replacements: List[Tuple[str, str]] = []

    # API keys → always checked, immediately fatal
    if "api_key" in detectors:
        api_hits = _detect_api_keys(text)
        if api_hits:
            raise PrivacyLeakError(
                f"API key detected in outbound payload (scope={scope!r}). "
                "This must never reach a remote service — check config.py."
            )

    if "ssn" in detectors:
        replacements.extend(_detect_ssn(text))
    if "ein" in detectors:
        replacements.extend(_detect_ein(text))
    if "credit_card" in detectors:
        replacements.extend(_detect_credit_cards(text))
    if "routing" in detectors:
        replacements.extend(_detect_routing(text))
    if "account" in detectors:
        replacements.extend(_detect_account_numbers(text))
    if "email" in detectors:
        replacements.extend(_detect_emails(text))
    if "phone" in detectors:
        replacements.extend(_detect_phones(text))
    if "address" in detectors:
        replacements.extend(_detect_addresses(text))
    # entity_name must run BEFORE person_name — exact match wins over heuristic
    if "entity_name" in detectors and eff_entity:
        replacements.extend(_detect_entity_name(text, eff_entity))
    if "person_name" in detectors:
        replacements.extend(_detect_person_names(text))
    if "counterparty" in detectors:
        replacements.extend(_detect_counterparty_names(text))

    # Apply longest match first to avoid partial replacements
    replacements.sort(key=lambda x: len(x[0]), reverse=True)

    # Deduplicate (same original may appear from multiple detectors)
    seen_originals: Set[str] = set()
    deduped: List[Tuple[str, str]] = []
    for orig, token in replacements:
        if orig not in seen_originals:
            seen_originals.add(orig)
            deduped.append((orig, token))

    mapping: RedactionMap = {}
    safe = text
    for orig, token in deduped:
        if orig in safe:
            safe = safe.replace(orig, token)
            # Only un-redactable tokens (sequential IDs, not ***) go in the map
            if "***" not in token:
                mapping[token] = orig

    # ── strict mode: post-sweep for any digit-run ≥ 7 ────────────────────────
    if AI_EGRESS_MODE == "strict":
        if re.search(r"\d{7,}", safe):
            raise PrivacyLeakError(
                f"strict mode: digit-run ≥ 7 remains in redacted payload "
                f"(scope={scope!r}). Extend detector coverage."
            )

    _log_egress_event(scope, len(deduped), "pass")
    return safe, mapping


def _log_egress_event(scope: str, token_count: int, result: str) -> None:
    log = _logging.getLogger("fi.privacy")
    log.debug(
        "egress_audit",
        extra={
            "event": "egress_audit",
            "scope": scope,
            "tokens_detected": token_count,
            "result": result,
        },
    )


# ── Unredact (return path — local display only) ───────────────────────────────

def unredact(text: str, mapping: RedactionMap) -> str:
    """
    Reverse-apply a RedactionMap to restore original values for local display.
    The map must never be persisted — it lives only in the calling stack frame.
    """
    if not mapping:
        return text
    result = text
    for token, original in mapping.items():
        result = result.replace(token, original)
    return result


def unredact_result(result: Dict[str, Any], mapping: RedactionMap) -> Dict[str, Any]:
    """
    Apply unredact() to all string values in a classification result dict.
    The 'reason' field is the primary field that may contain token references.
    """
    if not mapping:
        return result
    out = dict(result)
    for key, val in out.items():
        if isinstance(val, str):
            out[key] = unredact(val, mapping)
    return out


# ── audit_egress() — hard pre-flight check ───────────────────────────────────

def audit_egress(payload: Any) -> None:
    """
    Hard pre-flight check immediately before any outbound HTTP call.
    Scans the serialised payload for any PII that slipped through redact().
    Raises PrivacyLeakError and blocks the request if anything is found.

    This is the CI gate: it catches contributors who add a new outbound
    path without calling privacy.redact() first.

    Call this immediately before every requests.post / openai call / genai call.
    """
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, default=str)
    else:
        text = str(payload)

    # API keys → always fatal
    for pat in _API_KEY_PATTERNS:
        if pat.search(text):
            raise PrivacyLeakError(
                "audit_egress: API key detected in outbound payload. Blocked."
            )

    # High-confidence structural PII
    violations: List[str] = []

    if _SSN_HYPH.search(text):
        violations.append("SSN")
    if _EIN_PAT.search(text):
        violations.append("EIN")
    if _detect_credit_cards(text):
        violations.append("credit_card")
    if _detect_routing(text):
        violations.append("routing")

    if violations:
        raise PrivacyLeakError(
            f"audit_egress: PII detected in outbound payload: {violations}. "
            "Ensure privacy.redact() is called before issuing HTTP requests."
        )

    _log_egress_event("audit", 0, "pass")


# ── PrivacyFilter — logging integration ──────────────────────────────────────

class PrivacyFilter(_logging.Filter):
    """
    logging.Filter that redacts PII from log records before emit.
    Uses scope='log' (SSN, EIN, API keys only — high-precision, low-noise).

    Install in core/logging_setup.py so every handler is covered:
        handler.addFilter(PrivacyFilter())
    """

    def filter(self, record: _logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            safe, _ = redact(msg, scope="log")
            record.msg = safe
            record.args = ()
        except PrivacyLeakError:
            # API key in a log message — replace the entire message
            record.msg = "[REDACTED: API key detected in log message]"
            record.args = ()
        except Exception:
            # Never suppress a log record due to filter error
            pass
        return True


# ── privacy_status() — MCP tool helper ───────────────────────────────────────

_egress_event_log: List[Dict[str, Any]] = []
_MAX_EVENT_LOG = 10


def privacy_status() -> Dict[str, Any]:
    """
    Return current privacy configuration and recent egress audit events.
    Exposed as an MCP tool so operators can verify firewall status.
    Never includes original values — only token types and counts.
    """
    from config import AI_EGRESS_MODE

    return {
        "egress_mode": AI_EGRESS_MODE,
        "detector_categories": len(_ALL_DETECTORS),
        "session_tokens_issued": sum(_session_counters.values()),
        "allowlist_entries": len(_load_allowlist()),
        "recent_egress_events": _egress_event_log[-_MAX_EVENT_LOG:],
    }

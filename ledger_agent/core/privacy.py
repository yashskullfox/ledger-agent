"""
core/privacy.py  –  Enterprise PII / sensitive-data egress firewall (R-46)
───────────────────────────────────────────────────────────────────────────
Single chokepoint for all outbound text.  Tokenises PII before any remote
AI call and un-redacts on the return path.  The RedactionMap is NEVER
serialised, never written to disk, never logged.

Usage:
    from ledger_agent.core.privacy import redact, unredact, unredact_result, audit_egress

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
    FI_PARTNER_<n>_NAME    Partner names, token-replaced as <PERSON_nnn>
                           (entity names in the DB `entities` table are always
                           protected too — see "Protected names" below)
    FI_PRIVACY_NER         spacy  (activates spaCy NER; optional dependency)
"""
from __future__ import annotations

import functools
import json
import logging as _logging
import re
from pathlib import Path
from typing import Any

# ── Public types ─────────────────────────────────────────────────────────────

RedactionMap = dict[str, str]
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

_ALLOWLIST: set[str] | None = None


def _load_allowlist() -> set[str]:
    global _ALLOWLIST
    if _ALLOWLIST is not None:
        return _ALLOWLIST
    allowlist_path = Path(__file__).parent / "privacy_allowlist.txt"
    if allowlist_path.exists():
        entries: set[str] = set()
        for line in allowlist_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                entries.add(stripped.upper())
        _ALLOWLIST = entries
    else:
        _ALLOWLIST = set()
    return _ALLOWLIST


# ── Banking/system words — never redact as counterparties ─────────────────────

_SYSTEM_WORDS: set[str] = {
    "ACH", "ATM", "LLC", "INC", "CORP", "LTD", "DBA", "FBO",
    "REF", "CHK", "DEP", "PMT", "PAY", "TXN", "TRN", "EFT",
    "PPD", "CCD", "WEB", "TEL", "CTX", "IAT", "POS",
    "TRANSFER", "PAYMENT", "DEPOSIT", "WITHDRAWAL", "PURCHASE",  # redaction: allow (PURCHASE is a generic financial verb; substring 'chase' is incidental)
    "DEBIT", "CREDIT", "CHARGE", "REFUND", "FEE", "INTEREST",
    "BALANCE", "AVAILABLE", "PENDING", "CLEARED", "POSTED",
    "USD", "WIRE", "SWIFT", "IBAN", "BIC",
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    # Tech/protocol acronyms — frequent in audit/log lines, never partner names
    "JSON", "RPC", "MCP", "HTTP", "HTTPS", "TCP", "UDP", "TLS", "SSL",
    "API", "REST", "SOAP", "GRPC", "URL", "URI", "URN", "DNS", "DHCP",
    "CSV", "TSV", "PDF", "XML", "YAML", "TOML", "INI", "SQL", "ORM",
    "AWS", "GCP", "AZURE", "S3", "EC2", "EBS", "IAM", "VPC", "K8S",
    "CPU", "RAM", "GPU", "SSD", "HDD", "IO", "RPS", "QPS", "TPS",
    "SDK", "CLI", "GUI", "IDE", "OS", "OSX", "VM", "JVM", "JRE", "JDK",
    "GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD",
    "OK", "ERR", "ERROR", "WARN", "INFO", "DEBUG", "TRACE", "FATAL",
    "UTC", "GMT", "PST", "EST", "CST", "MST",
    "PII", "GDPR", "CCPA", "HIPAA", "SOX", "PCI", "DSS",
    "IRS", "SSN", "EIN", "ITIN", "DOB", "ZIP",
}


# ── API key / secret patterns (expanded — defence-in-depth) ──────────────────

_API_KEY_PATTERNS: list[re.Pattern] = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),                 # OpenAI / Anthropic
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),             # Anthropic prefixed
    re.compile(r"AIza[A-Za-z0-9_\-]{35}"),                 # Google / Gemini
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),          # Slack (any flavour)
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),                   # GitHub PAT
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),           # GitHub fine-grained
    re.compile(r"gho_[A-Za-z0-9]{36,}"),                   # GitHub OAuth
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),              # GitLab PAT
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                   # AWS Access Key ID
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),                   # AWS STS temp Key ID
    re.compile(r"aws_secret_access_key\s*[:=]\s*[A-Za-z0-9/+=]{40}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b", re.IGNORECASE),  # bearer tokens
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"\b[a-f0-9]{32,64}\b(?=[^\w]|$)(?=.*\b(?:secret|token|key|password)\b)",
               re.IGNORECASE),  # hex secrets with hint word
]

# ── SSN ───────────────────────────────────────────────────────────────────────

_SSN_HYPH = re.compile(r"\b(\d{3})-(\d{2})-(\d{4})\b")
_SSN_BARE_CTX = re.compile(r"\b(?:SSN|TIN|TAX[\s\-]?ID|SOCIAL[\s\-]?SECURITY)\b", re.IGNORECASE)

# ── EIN ───────────────────────────────────────────────────────────────────────
# BUG-P3 fix: context-gate EIN detection to avoid false positives on
# date-like patterns such as "2024-1234567" (fiscal year + transaction ID).

_EIN_PAT = re.compile(r"\b(\d{2})-(\d{7})\b")

_EIN_CTX = re.compile(
    r"\b(?:EIN|FEIN|EMPLOYER\s+ID(?:ENTIFICATION)?|FEDERAL\s+TAX\s+ID"
    r"|TAX\s+ID(?:ENTIFICATION)?|TIN|E\.I\.N\.?)\b",
    re.IGNORECASE,
)

# ── Credit card PAN ───────────────────────────────────────────────────────────

_CARD_CANDIDATE = re.compile(r"(?<!\d)\d(?:[ \-]?\d){12,18}(?!\d)")

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
# BUG-P1 fix: require a phone-context keyword within ±60 chars to avoid
# false-positive matches on 10-digit invoice numbers, Unix epoch values,
# transaction IDs etc. that appear in audit log kwargs.

_PHONE_PAT = re.compile(
    r"(?<!\d)"
    r"(?:(?:\+1[\s\-.]?)?)?"
    r"(?:\(?\d{3}\)?[\s\-.]?)"
    r"\d{3}[\s\-.]?"
    r"\d{4}"
    r"(?!\d)"
)

_PHONE_CTX = re.compile(
    r"\b(?:PHONE|TEL(?:EPHONE)?|MOBILE|CELL|FAX|CONTACT|CALL|REACH|DIRECT"
    r"|EXT(?:ENSION)?|VOICEMAIL|HOTLINE|HELPDESK)\b",
    re.IGNORECASE,
)

# ── Street address ────────────────────────────────────────────────────────────

_ADDR_PAT = re.compile(
    r"\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+"
    r"(?:St(?:reet)?|Ave(?:nue)?|Rd|Road|Blvd|Boulevard|Dr(?:ive)?|"
    r"Ln|Lane|Ct|Court|Way|Pl(?:ace)?|Pkwy|Hwy|Highway)\b",
    re.IGNORECASE,
)

# ── IBAN (international bank account) ─────────────────────────────────────────
# Country code + 2 check digits + 11–30 alphanumeric (max length 34).
_IBAN_PAT = re.compile(
    r"\b([A-Z]{2}\d{2}(?:[\s\-]?[A-Z0-9]{4}){2,7}(?:[\s\-]?[A-Z0-9]{1,4})?)\b"
)

# ── SWIFT / BIC code (8 or 11 chars) ──────────────────────────────────────────
_SWIFT_PAT = re.compile(r"\b([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b")
_SWIFT_CTX = re.compile(r"\b(?:SWIFT|BIC)\b", re.IGNORECASE)

# ── ITIN (begins with 9, format 9NN-NN-NNNN with middle group 50-65, 70-88, 90-92, 94-99) ─
_ITIN_PAT = re.compile(r"\b9\d{2}-(?:5[0-9]|6[0-5]|7[0-9]|8[0-8]|9[0-2]|9[4-9])-\d{4}\b")

# ── Driver's license (heuristic — alpha prefix + 6-12 digits, context-gated) ──
# We can't use variable-width look-behind in the stdlib re engine, so context
# is enforced manually by _detect_driver_license() via _DL_CTX scanning a
# ±40-char window around each candidate.
_DL_CTX = re.compile(r"\b(?:DL|DRIVER[\s'-]?S?|LICENSE|LIC#?)\b", re.IGNORECASE)
_DL_CANDIDATE = re.compile(r"\b([A-Z]\d{6,12})\b")

# ── Passport (heuristic: 1-2 letters + 6-9 digits, context-gated) ─────────────
_PASSPORT_CTX = re.compile(r"\bPASSPORT\b", re.IGNORECASE)
_PASSPORT_CANDIDATE = re.compile(r"\b([A-Z]{1,2}\d{6,9})\b")

# ── Date of birth (multiple formats, context-gated) ───────────────────────────
_DOB_CTX = re.compile(
    r"\b(?:DOB|D\.O\.B\.|DATE\s+OF\s+BIRTH|BIRTH[\s\-]?DATE|BORN)\b",
    re.IGNORECASE,
)
_DOB_DATE = re.compile(
    r"\b("
    r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"           # 01/15/1989 or 01-15-89
    r"|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}"             # 1989-01-15
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}"
    r")\b",
    re.IGNORECASE,
)

# ── ZIP+4 ─────────────────────────────────────────────────────────────────────
_ZIP4_PAT = re.compile(r"\b(\d{5})-(\d{4})\b")

# ── IPv4 / IPv6 ───────────────────────────────────────────────────────────────
_IPV4_PAT = re.compile(
    r"\b((?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)){3})\b"
)
_IPV6_PAT = re.compile(
    r"(?<![:\w])([0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){7})(?![:\w])"
)

# ── MAC address ───────────────────────────────────────────────────────────────
_MAC_PAT = re.compile(r"\b([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b")

# ── URL with embedded credentials ─────────────────────────────────────────────
_URL_CRED_PAT = re.compile(
    r"\b([a-z][a-z0-9+.\-]{1,30}://[^/\s:@]+:[^/\s@]+@[A-Za-z0-9.\-]+)"
)

# ── Person name (context-gated heuristic) ─────────────────────────────────────
# Context keywords are case-insensitive via inline (?i:...) group.
# The name capture group is intentionally case-sensitive so "LLC" / "CORP"
# (all-caps) are excluded — proper names have lowercase letters after the initial.

_PERSON_CTX_PAT = re.compile(
    # Banking / payments context
    r"(?i:\b(?:PAYEE|BENEFICIARY|BENE|PAY\s+TO(?:\s+THE\s+ORDER\s+OF)?|"
    r"PAID\s+TO|PAYMENT\s+TO|TRANSFER\s+TO|SENT\s+TO|FROM|"
    # Customer / partner / personnel context
    r"CUSTOMER|CLIENT|MEMBER|PARTNER|OWNER|EMPLOYEE|CONTRACTOR|"
    r"AUTHORIZED\s+SIGNER|SIGNER|PREPARED\s+BY|FILED\s+BY|"
    # Honorific / form context
    r"MR|MRS|MS|MISS|DR|PROF|HON|"
    r"NAME|FULL\s+NAME|TAXPAYER|PARTNER\s+NAME)\s*[:\.,]?\s+)"
    r"([A-Z][a-z]{1,20}(?:\s+[A-Z]\.?)?"               # first [middle init]
    r"(?:\s+[A-Z][a-z]{1,20}){1,2})",                   # last [optional suffix]
)

# ── ALL-CAPS counterparty business name ───────────────────────────────────────
# BUG-P2 fix: pattern now allows ONE or more all-caps tokens (changed `+` to `*`
# on the trailing group). A bare single token (e.g. an entity acronym or a bank
# name) would previously slip through because the old pattern required at least
# two words.
# The existing filters inside _detect_counterparty_names still apply:
#   • len(orig) < 5 — drops short acronyms (<5 chars) like "IRS", "ACH", "LLC"
#   • _SYSTEM_WORDS — drops known protocol/financial acronyms
#   • _COMPANY_SUFFIXES — drops suffix-only matches
#   • allowlist — drops user-configured vendor names
# These guards make the single-word path low-noise in practice.

_ALLCAPS_SEQ_PAT = re.compile(r"\b([A-Z]{2,}(?:[\s&][A-Z]{2,})*)\b")


# ── Per-session pseudonym registry ────────────────────────────────────────────
# Maps (category, original_value) → token.
# NEVER persisted; regenerated each process invocation.

_session_map: dict[tuple[str, str], str] = {}
_session_counters: dict[str, int] = {}


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

def _detect_api_keys(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for pat in _API_KEY_PATTERNS:
        for m in pat.finditer(text):
            hits.append((m.group(0), "<SECRET_***>"))
    return hits


def _detect_ssn(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()

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


def _detect_ein(text: str) -> list[tuple[str, str]]:
    # BUG-P3 fix: require an EIN-context keyword within ±60 chars.
    hits: list[tuple[str, str]] = []
    for m in _EIN_PAT.finditer(text):
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 60)
        window = text[start:end]
        if _EIN_CTX.search(window):
            hits.append((m.group(0), "<EIN_***>"))
    return hits


def _detect_credit_cards(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for m in _CARD_CANDIDATE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"[ \-]", "", raw)
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            last4 = digits[-4:]
            token = _stable_token("CARD", digits, f"xxx{last4}")
            hits.append((raw, token))
    return hits


def _detect_routing(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
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


def _detect_account_numbers(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
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


def _detect_emails(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for m in _EMAIL_PAT.finditer(text):
        orig = m.group(0)
        token = _stable_token("EMAIL", orig)
        hits.append((orig, token))
    return hits


def _detect_phones(text: str) -> list[tuple[str, str]]:
    # BUG-P1 fix: require a phone-context keyword within ±60 chars to prevent
    # false-positive matches on 10-digit invoice numbers, Unix epoch values,
    # transaction IDs, and fiscal-year compounds in audit log fields.
    hits: list[tuple[str, str]] = []
    for m in _PHONE_PAT.finditer(text):
        orig = m.group(0)
        digits = re.sub(r"\D", "", orig)
        if len(digits) < 10:
            continue
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 60)
        window = text[start:end]
        if not _PHONE_CTX.search(window):
            continue
        token = _stable_token("PHONE", orig)
        hits.append((orig, token))
    return hits


def _detect_addresses(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for m in _ADDR_PAT.finditer(text):
        orig = m.group(0)
        token = _stable_token("ADDR", orig)
        hits.append((orig, token))
    return hits


def _detect_person_names(text: str) -> list[tuple[str, str]]:
    allowlist = _load_allowlist()
    hits: list[tuple[str, str]] = []
    for m in _PERSON_CTX_PAT.finditer(text):
        # Group 1 is the name (the (?i:...) context part is a non-capturing group)
        name = m.group(1).strip()
        if name.upper() in allowlist:
            continue
        token = _stable_token("PERSON", name)
        hits.append((name, token))
    return hits


# ── Company-name suffixes (these alone don't disqualify a counterparty match) ─

_COMPANY_SUFFIXES: set[str] = {
    "LLC", "INC", "CORP", "LTD", "DBA", "CO", "LP", "LLP", "PC", "PA",
    "PLLC", "PLC", "NV", "SA", "AG", "GMBH",
}


def _detect_counterparty_names(text: str) -> list[tuple[str, str]]:
    allowlist = _load_allowlist()
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
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


def _detect_entity_name(text: str, entity_name: str) -> list[tuple[str, str]]:
    """Legacy single-name detector (kept for callers); redact() uses protected names."""
    if not entity_name:
        return []
    pattern = re.compile(re.escape(entity_name), re.IGNORECASE)
    hits: list[tuple[str, str]] = []
    for m in pattern.finditer(text):
        hits.append((m.group(0), ENTITY_TOKEN))
    return hits


# ── Protected names (P-04): entity + partner names, always redacted ──────────
# Sources, merged on every call:
#   • every name in the DB ``entities`` table (read-only; the DB is never created)
#   • FI_PRIVACY_ENTITY_NAME (config + env) and an explicit redact(entity_name=)
#   • FI_PARTNER_<n>_NAME (env; api._build_partners uses n = 1, 2)
# Matching is case-insensitive and whitespace-collapsed on the full name, plus
# every individual name token of 4+ letters that is not a generic business word
# (_NAME_STOPWORDS) or a pseudonym placeholder (anything containing "_" or a
# digit, e.g. ENTITY_A).  Word boundaries are Unicode-aware (letters, digits,
# combining marks and "_" count as word characters); scripts written without
# spaces (CJK, kana, Thai, …) match without a boundary check.
# Replacement tokens never contain any part of the name:
#   entity names → <ENTITY_NAME>       partner names → <PERSON_nnn> (stable)

ENTITY_TOKEN = "<ENTITY_NAME>"

_NAME_STOPWORDS: frozenset[str] = frozenset({
    # Legal-form / structural words
    "LLC", "INC", "CORP", "CORPORATION", "INCORPORATED", "LIMITED", "COMPANY",
    "HOLDING", "HOLDINGS", "GROUP", "CAPITAL", "PARTNER", "PARTNERS", "PARTNERSHIP",
    "TRUST", "BANK", "FUND", "FUNDS", "ENTITY",
    # Generic business words common in entity names and in report labels
    "ASSOCIATES", "ENTERPRISES", "VENTURES", "INVESTMENT", "INVESTMENTS",
    "MANAGEMENT", "SERVICES", "SOLUTIONS", "CONSULTING", "TRADING", "FINANCIAL",
    "ADVISORS", "EQUITY", "ASSETS", "GLOBAL", "INTERNATIONAL",
})

# Scripts written without spaces: a name inside them has no word boundary.
_NO_SPACE_SCRIPTS = ("CJK", "HIRAGANA", "KATAKANA", "THAI", "LAO", "KHMER", "MYANMAR")

_PARTNER_ENV_RE = re.compile(r"^FI_PARTNER_\d+_NAME$")

# DB-sourced names, cached per (db path, file fingerprint).
_db_names_key: tuple | None = None
_db_names: tuple[str, ...] = ()


def _is_word_char(ch: str) -> bool:
    import unicodedata
    return ch.isalnum() or ch == "_" or unicodedata.category(ch).startswith("M")


def _is_no_space_script(ch: str) -> bool:
    import unicodedata
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return False
    return name.startswith(_NO_SPACE_SCRIPTS)


def _canonical_name(name: object) -> str:
    return " ".join(str(name).split())


def _name_tokens(name: str) -> list[str]:
    """Individual protected tokens of a name (4+ letters, not generic/placeholder)."""
    import unicodedata
    out: list[str] = []
    for tok in re.split(r"[\s,.;:/&()\"'\-]+", name):
        if len(tok) < 4 or tok.upper() in _NAME_STOPWORDS:
            continue
        # letters / combining marks only — drops ENTITY_A, PARTNER_1, digits
        if not all(unicodedata.category(c)[0] in "LM" for c in tok):
            continue
        out.append(tok)
    return out


def _full_name_is_meaningful(name: str) -> bool:
    """False for names made only of generic words / initials ("Entity", "Partner A")."""
    toks = [t for t in re.split(r"[\s,.;:/&()\"'\-]+", name) if t]
    return any(len(t) > 1 and t.upper() not in _NAME_STOPWORDS and not t.isdigit()
               for t in toks)


def _resolve_db_path_for_names() -> Path | None:
    try:
        from ledger_agent.core.database import _resolve_db_path
        return Path(_resolve_db_path(None)).expanduser().resolve()
    except Exception:
        return None


def _db_fingerprint(path: Path) -> tuple:
    parts: list[Any] = [str(path)]
    for p in (path, Path(str(path) + "-wal")):
        try:
            st = p.stat()
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    return tuple(parts)


def _load_db_entity_names() -> tuple[str, ...]:
    """Entity names from the DB ``entities`` table, cached.

    Read-only (``mode=ro``): a missing DB is never created.  Any error falls
    back to an empty list (env-only protection) and logs at DEBUG without values.
    The cache key is the DB path plus its file fingerprint, so a change of
    FI_DB_PATH or a write to the DB invalidates it.
    """
    global _db_names_key, _db_names
    path = _resolve_db_path_for_names()
    if path is None or not path.is_file():
        return ()
    key = _db_fingerprint(path)
    if _db_names_key == key:
        return _db_names
    # Store an empty result first so a re-entrant call (e.g. logging → redact)
    # cannot recurse into the DB read.
    _db_names_key, _db_names = key, ()
    names: tuple[str, ...] = ()
    try:
        import sqlite3
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT name FROM entities").fetchall()
        finally:
            conn.close()
        names = tuple(sorted({_canonical_name(r[0]) for r in rows if r and r[0]}))
    except Exception as exc:  # noqa: BLE001 — fail to env-only protection
        _logging.getLogger("fi.privacy").debug(
            "protected_names: DB entity read failed (%s); using env names only",
            type(exc).__name__,
        )
        names = ()
    _db_names = names
    return names


def refresh_protected_names() -> None:
    """Drop cached protected names (call after entity changes or in tests)."""
    global _db_names_key, _db_names
    _db_names_key, _db_names = None, ()
    _compile_protected.cache_clear()


def _env_entity_names(explicit: str = "") -> list[str]:
    import os
    names: list[str] = []
    try:
        from config import PRIVACY_ENTITY_NAME
    except Exception:  # noqa: BLE001
        PRIVACY_ENTITY_NAME = ""  # noqa: N806
    for n in (explicit, PRIVACY_ENTITY_NAME, os.environ.get("FI_PRIVACY_ENTITY_NAME", "")):
        if n and n.strip():
            names.append(_canonical_name(n))
    return names


def _env_partner_names() -> list[str]:
    import os
    names = []
    for key in sorted(os.environ):
        if _PARTNER_ENV_RE.match(key):
            val = os.environ.get(key, "")
            if val and val.strip():
                names.append(_canonical_name(val))
    return names


def get_protected_names(entity_name: str = "") -> list[tuple[str, str]]:
    """Return [(kind, canonical full name)] with kind ∈ {"entity", "person"}.

    Values are for in-process matching only — never log, print or persist them.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    entities = [*_env_entity_names(entity_name), *_load_db_entity_names()]
    for kind, names in (("entity", entities), ("person", _env_partner_names())):
        for n in names:
            folded = n.casefold()
            if folded in seen or not _full_name_is_meaningful(n):
                continue
            seen.add(folded)
            out.append((kind, n))
    return out


def _needle_regex(needle: str) -> re.Pattern:
    body = r"\s+".join(re.escape(p) for p in needle.split())
    return re.compile(body, re.IGNORECASE | re.UNICODE)


@functools.lru_cache(maxsize=64)
def _compile_protected(
    names: tuple[tuple[str, str], ...],
) -> tuple[tuple[re.Pattern, str, str], ...]:
    """(regex, kind, owner) for every full name and protected token, longest first."""
    entries: dict[str, tuple[str, str]] = {}
    for kind, full in names:
        for needle in (full, *_name_tokens(full)):
            entries.setdefault(needle.casefold(), (needle, kind + "\x00" + full))
    ordered = sorted(entries.values(), key=lambda e: len(e[0]), reverse=True)
    out = []
    for needle, tag in ordered:
        kind, owner = tag.split("\x00", 1)
        out.append((_needle_regex(needle), kind, owner))
    return tuple(out)


def _boundary_ok(text: str, start: int, end: int) -> bool:
    if start > 0:
        first, prev = text[start], text[start - 1]
        if _is_word_char(prev) and not (_is_no_space_script(first)
                                        or _is_no_space_script(prev)):
            return False
    if end < len(text):
        last, nxt = text[end - 1], text[end]
        if _is_word_char(nxt) and not (_is_no_space_script(last)
                                       or _is_no_space_script(nxt)):
            return False
    return True


def _find_protected_spans(
    text: str, entity_name: str = "",
) -> list[tuple[int, int, str, str]]:
    """Non-overlapping (start, end, kind, owner) matches of protected names."""
    names = tuple(get_protected_names(entity_name))
    if not names or not text:
        return []
    taken: list[tuple[int, int, str, str]] = []
    for pat, kind, owner in _compile_protected(names):
        for m in pat.finditer(text):
            s, e = m.span()
            if not _boundary_ok(text, s, e):
                continue
            if any(s < te and ts < e for ts, te, _k, _o in taken):
                continue
            taken.append((s, e, kind, owner))
    taken.sort()
    return taken


def _protected_token(kind: str, owner: str) -> str:
    if kind == "entity":
        return ENTITY_TOKEN
    return _stable_token("PERSON", owner.casefold())


def _redact_protected_names(text: str, entity_name: str = "") -> tuple[str, RedactionMap, int]:
    """Replace protected names with tokens.  Returns (text, mapping, hit count)."""
    spans = _find_protected_spans(text, entity_name)
    if not spans:
        return text, {}, 0
    mapping: RedactionMap = {}
    pieces: list[str] = []
    pos = 0
    for s, e, kind, owner in spans:
        token = _protected_token(kind, owner)
        pieces.append(text[pos:s])
        pieces.append(token)
        mapping[token] = text[s:e]
        pos = e
    pieces.append(text[pos:])
    return "".join(pieces), mapping, len(spans)


def contains_protected_name(text: str) -> bool:
    """True if any protected full name or protected token occurs in text."""
    return bool(_find_protected_spans(text))


def _detect_iban(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for m in _IBAN_PAT.finditer(text):
        raw = m.group(0)
        compact = re.sub(r"[\s\-]", "", raw)
        # Length sanity: IBANs are 15-34 chars
        if 15 <= len(compact) <= 34:
            hits.append((raw, "<IBAN_***>"))
    return hits


def _detect_swift(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for m in _SWIFT_PAT.finditer(text):
        orig = m.group(0)
        start = max(0, m.start() - 40)
        end = min(len(text), m.end() + 40)
        window = text[start:end]
        if _SWIFT_CTX.search(window):
            hits.append((orig, "<SWIFT_***>"))
    return hits


def _detect_itin(text: str) -> list[tuple[str, str]]:
    return [(m.group(0), "<ITIN_***>") for m in _ITIN_PAT.finditer(text)]


def _detect_driver_license(text: str) -> list[tuple[str, str]]:
    """Context-gated: only fires if 'DL', 'DRIVER', 'LICENSE' nearby."""
    if not _DL_CTX.search(text):
        return []
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _DL_CANDIDATE.finditer(text):
        orig = m.group(0)
        if orig in seen:
            continue
        start = max(0, m.start() - 40)
        end = min(len(text), m.end() + 40)
        if _DL_CTX.search(text[start:end]):
            seen.add(orig)
            hits.append((orig, "<DL_***>"))
    return hits


def _detect_passport(text: str) -> list[tuple[str, str]]:
    """Context-gated to passport keyword."""
    if not _PASSPORT_CTX.search(text):
        return []
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _PASSPORT_CANDIDATE.finditer(text):
        orig = m.group(0)
        if orig in seen:
            continue
        start = max(0, m.start() - 40)
        end = min(len(text), m.end() + 40)
        if _PASSPORT_CTX.search(text[start:end]):
            seen.add(orig)
            hits.append((orig, "<PASSPORT_***>"))
    return hits


def _detect_dob(text: str) -> list[tuple[str, str]]:
    """Context-gated DOB; date alone is not redacted."""
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for ctx in _DOB_CTX.finditer(text):
        start = ctx.end()
        window = text[start : min(len(text), start + 60)]
        for m in _DOB_DATE.finditer(window):
            orig = m.group(0)
            if orig in seen:
                continue
            seen.add(orig)
            hits.append((orig, "<DOB_***>"))
    return hits


def _detect_zip4(text: str) -> list[tuple[str, str]]:
    return [(m.group(0), "<ZIP4_***>") for m in _ZIP4_PAT.finditer(text)]


def _detect_ipv4(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _IPV4_PAT.finditer(text):
        orig = m.group(0)
        # Skip common non-PII addresses (loopback, link-local, RFC 1918 private,
        # multicast, broadcast). Operators want these visible for diagnostics.
        first = int(orig.split(".")[0])
        if (orig.startswith("127.") or orig.startswith("0.")
                or orig == "255.255.255.255" or 224 <= first <= 239):
            continue
        if orig in seen:
            continue
        seen.add(orig)
        hits.append((orig, _stable_token("IPV4", orig)))
    return hits


def _detect_ipv6(text: str) -> list[tuple[str, str]]:
    return [(m.group(0), _stable_token("IPV6", m.group(0)))
            for m in _IPV6_PAT.finditer(text)]


def _detect_mac(text: str) -> list[tuple[str, str]]:
    return [(m.group(0), _stable_token("MAC", m.group(0)))
            for m in _MAC_PAT.finditer(text)]


def _detect_url_creds(text: str) -> list[tuple[str, str]]:
    """URLs with `scheme://user:pass@host` — always fatal-class secrets."""
    return [(m.group(0), "<URL_WITH_CREDS_***>") for m in _URL_CRED_PAT.finditer(text)]


# ── Scope → active detector set ──────────────────────────────────────────────

_ALL_DETECTORS = [
    "api_key", "ssn", "ein", "itin", "credit_card", "routing", "account",
    "iban", "swift", "email", "phone", "address", "zip4",
    "driver_license", "passport", "dob",
    "ipv4", "ipv6", "mac", "url_creds",
    "entity_name", "person_name", "counterparty",
]

# "entity_name" enables protected-name redaction (DB entity names + env
# entity/partner names, P-04).  Unknown scopes fall back to _ALL_DETECTORS, so
# every egress scope gets it; the remote-backend scopes are listed explicitly.
_SCOPE_DETECTORS: dict[str, list[str]] = {
    "openai":            _ALL_DETECTORS,
    "openai_compatible": _ALL_DETECTORS,
    "gemini":            _ALL_DETECTORS,
    "anthropic":         _ALL_DETECTORS,
    "ollama":            _ALL_DETECTORS,
    "ai_classification": _ALL_DETECTORS,
    "ai_context":        _ALL_DETECTORS,
    "mcp_response":      _ALL_DETECTORS,
    "memory_file":  [
        "api_key", "ssn", "ein", "itin", "credit_card", "routing",
        "account", "iban", "swift", "email", "phone",
        "driver_license", "passport", "dob",
        "url_creds", "entity_name", "person_name",
    ],
    # Log scope tightened: previously SSN/EIN/API-keys only; now mirrors the
    # full structural-identifier set + network identifiers + counterparty names
    # so audit/log lines never leak the substance of a record even if a caller
    # forgets to redact first. Person-name remains context-gated (needs titles
    # like "Mr/Ms/Customer:") to keep false-positive noise tolerable.
    "log": [
        "api_key", "ssn", "ein", "itin", "credit_card", "routing", "account",
        "iban", "swift", "email", "phone", "address", "zip4",
        "driver_license", "passport", "dob",
        "ipv4", "ipv6", "mac", "url_creds",
        "entity_name", "person_name", "counterparty",
    ],
}


# ── Main redact() function ────────────────────────────────────────────────────

def redact(
    text: str,
    scope: str = "openai",
    entity_name: str = "",
) -> tuple[str, RedactionMap]:
    """
    Tokenise PII in `text` before sending to a remote service.

    Returns (safe_text, RedactionMap).
    The RedactionMap maps token → original for the unredact() return path.
    It is NEVER serialised or written to disk.

    Raises PrivacyLeakError if API keys are found (should never occur —
    defence-in-depth above config.py's build-time secret guard).
    """
    from config import AI_EGRESS_MODE, AI_EGRESS_MODE_ACK

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

    replacements: list[tuple[str, str]] = []

    # API keys → always checked, immediately fatal
    if "api_key" in detectors:
        api_hits = _detect_api_keys(text)
        if api_hits:
            raise PrivacyLeakError(
                f"API key detected in outbound payload (scope={scope!r}). "
                "This must never reach a remote service — check config.py."
            )

    # Protected names (entity names from the DB + env entity/partner names) run
    # FIRST, with Unicode word boundaries, before any heuristic detector — an
    # exact name match wins over the person/counterparty heuristics (P-04).
    name_map: RedactionMap = {}
    name_hits = 0
    if "entity_name" in detectors:
        text, name_map, name_hits = _redact_protected_names(text, entity_name)

    if "ssn" in detectors:
        replacements.extend(_detect_ssn(text))
    if "ein" in detectors:
        replacements.extend(_detect_ein(text))
    if "itin" in detectors:
        replacements.extend(_detect_itin(text))
    if "credit_card" in detectors:
        replacements.extend(_detect_credit_cards(text))
    if "iban" in detectors:
        replacements.extend(_detect_iban(text))
    if "swift" in detectors:
        replacements.extend(_detect_swift(text))
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
    if "zip4" in detectors:
        replacements.extend(_detect_zip4(text))
    if "driver_license" in detectors:
        replacements.extend(_detect_driver_license(text))
    if "passport" in detectors:
        replacements.extend(_detect_passport(text))
    if "dob" in detectors:
        replacements.extend(_detect_dob(text))
    if "ipv4" in detectors:
        replacements.extend(_detect_ipv4(text))
    if "ipv6" in detectors:
        replacements.extend(_detect_ipv6(text))
    if "mac" in detectors:
        replacements.extend(_detect_mac(text))
    if "url_creds" in detectors:
        replacements.extend(_detect_url_creds(text))
    if "person_name" in detectors:
        replacements.extend(_detect_person_names(text))
    if "counterparty" in detectors:
        replacements.extend(_detect_counterparty_names(text))

    # Apply longest match first to avoid partial replacements
    replacements.sort(key=lambda x: len(x[0]), reverse=True)

    # Deduplicate (same original may appear from multiple detectors)
    seen_originals: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for orig, token in replacements:
        if orig not in seen_originals:
            seen_originals.add(orig)
            deduped.append((orig, token))

    mapping: RedactionMap = dict(name_map)
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

    _log_egress_event(scope, len(deduped) + name_hits, "pass")
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


def unredact_result(result: dict[str, Any], mapping: RedactionMap) -> dict[str, Any]:
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
        # Unescaped copy so non-ASCII protected names are visible to the scan.
        text_unicode = json.dumps(payload, default=str, ensure_ascii=False)
    else:
        text = str(payload)
        text_unicode = text

    # API keys → always fatal
    for pat in _API_KEY_PATTERNS:
        if pat.search(text):
            raise PrivacyLeakError(
                "audit_egress: API key detected in outbound payload. Blocked."
            )

    # High-confidence structural PII (every detector below is high-precision
    # enough that any hit in an outbound payload is grounds to fail the call).
    violations: list[str] = []

    if _SSN_HYPH.search(text):
        violations.append("SSN")
    if _EIN_PAT.search(text):
        violations.append("EIN")
    if _ITIN_PAT.search(text):
        violations.append("ITIN")
    if _detect_credit_cards(text):
        violations.append("credit_card")
    if _detect_routing(text):
        violations.append("routing")
    if _detect_iban(text):
        violations.append("IBAN")
    if _URL_CRED_PAT.search(text):
        violations.append("URL_credentials")
    # private-key blocks should NEVER appear in an outbound payload
    if "-----BEGIN " in text and "PRIVATE KEY-----" in text:
        violations.append("private_key_block")
    # Protected entity / partner names (P-04).  Category only — never the value.
    try:
        if contains_protected_name(text_unicode):
            violations.append("protected_name")
    except Exception as exc:  # noqa: BLE001 — fail closed
        raise PrivacyLeakError(
            "audit_egress: protected-name check failed. Blocked."
        ) from exc

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

_egress_event_log: list[dict[str, Any]] = []
_MAX_EVENT_LOG = 10


def privacy_status() -> dict[str, Any]:
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

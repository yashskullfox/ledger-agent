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

Account numbers (P-05) — no account number, full, partial, masked or last-4,
may leave the host:
    Tokens          <ACCT_nnn> (bank accounts, masked refs, bare digit runs) and
                    <CARD_nnn> (Luhn-valid PANs) carry only a per-session counter,
                    never any digit of the original.  Stable per session: the
                    same digits always map to the same token (``x1234`` and
                    ``acct 1234`` share one).  Every scope uses this format; there
                    is no local-display scope that keeps last-4.
    partial_account Marker + 4 or more digits, case-insensitive, Unicode-aware:
                    x1234, XXXX-1234, *1234 (any run of stars), ...1234, …1234, acct 1234,
                    acct# 1234, acct no. 1234, account ending (in) 1234,
                    ending in 1234 — the grammar shared with owner_draws
                    (ACCOUNT_MARKER_PATTERN) — plus firewall-only extras: bullet
                    masks (••••1234), "card/checking/savings ending 1234",
                    "last 4 (digits) 1234", and Konto/Kto/cuenta/cta/compte/conta.
                    A marker-anchored group is redacted even if it looks like a
                    year (a real last-4 can be any 4 digits).  A bare "ending
                    1234" (e.g. "period ending 2025") is not a marker.
    digit_run       8+ consecutive digits, or 3–6-digit groups joined by one
                    consistent separator (single space or hyphen) totalling 8+
                    digits ("1234 5678 9012", "1234-5678"), with no keyword
                    needed.  Not matched:
                      • decimal amounts — an ungrouped run of at most 10
                        digits followed by "." or "," + digit (12345678.90) —
                        and the fractional part of an amount; a longer run
                        before a decimal point is still redacted;
                      • currency-prefixed amounts ($12345678, € 12345678);
                      • 8-digit dates — the digits parse as a valid YYYYMMDD,
                        MMDDYYYY or DDMMYYYY date with year 1900–2099 (20260115,
                        01152026, 2026-0115);
                      • compact timestamps YYYYMMDDHHMM[SS] with a valid date
                        and time; ISO timestamps (2026-01-15T10:30:00) never form
                        a candidate (2-digit groups);
                      • two space-separated numbers ("2026 1500" is a year and
                        an amount) — space-grouped forms need 3+ groups;
                      • digits inside a hex id / UUID / digest (a run of 16+
                        hex characters containing a letter a–f).
                    Precedence: card, SSN, ZIP+4, phone, IBAN, … run first; on an
                    identical span their token wins, and a longer match from any
                    detector is replaced before a shorter one.
    Scopes          Both detectors run in every egress scope (openai, gemini,
                    anthropic, openai_compatible, ollama, ai_classification,
                    ai_context, mcp_response, unknown scopes), in memory_file
                    and in ``log`` (stderr / audit files): stderr is inherited
                    by the Java webapp, so it is treated as egress (P-15).
    audit_egress()  Blocks any payload that still contains a marker + 4-digit
                    reference ("account_reference") or a bare digit run
                    ("digit_run") — category only, never the value.

Person and counterparty names (P-07) — heuristic, Unicode-aware, any language:
    Words           Letter (+ combining mark) runs of any script.  A word glued
                    to a digit or "_" (ENTITY_A, <PERSON_001>, S3) is never a name.
    Vocabulary      Exempt words are never redacted, each BY ITSELF: neighbours in
                    the same run are still evaluated.  Sources: allowlist one-word
                    entries (multi-word entries such as "Wells Fargo" are exempt
                    only as the whole phrase), _SYSTEM_WORDS, _GENERIC_WORDS
                    (banking / bookkeeping words in en, de, es, fr, pt, ru), the
                    context words below, the default COA names + keywords
                    (securities rows excluded) and the upper-case words of the
                    local classifier's rules (they cover its fuzzy vendor list).
                    The COA and vendor words are lazily imported; if the import
                    fails (re-entrant import) only the static lists apply.
    Counterparty    ALL-CAPS runs in any cased script (Latin, Cyrillic, Greek;
                    "ß" counts as caps) → <COUNTERPARTY_nnn>.  A run is split at
                    every exempt word; a piece needs 3+ name letters if split off,
                    5+ characters if the run was not split (short acronyms stay).
                    "ZELLE PAYMENT TO JORDAN SAMPLEPERSON" keeps the app name and
                    "PAYMENT TO" and redacts the name.
    Person, cased   After a context word, 1–4 capitalised words (Title Case or ALL
                    CAPS, initials and particles de/von/van/… allowed inside) →
                    <PERSON_nnn>, or <COUNTERPARTY_nnn> when a company suffix
                    (Corp, GmbH, …) is part of / follows the name.  Leading exempt
                    words are skipped (max 2), the name stops at the next one.
                      strong (1 word): a payment noun, optionally one more word,
                        then a payee preposition — "Zelle payment to", "Zahlung
                        an", "Gutschrift von", "Pago a", "Transferencia de",
                        "Virement à", "Pix para", "Перевод для" — or a payee
                        label: Payee, Beneficiary, Pay to the order of,
                        Empfänger, Beneficiario, Bénéficiaire, Favorecido, …
                      weak (2 words): bare "to" / "from" and personnel or
                        honorific words (Customer, Partner, Mr, Herr, Sra, …).
    Person, uncased CJK, kana, Hangul, Devanagari, Arabic, Hebrew, Thai have no
                    capitals, so a name is taken only next to a payment / transfer
                    word (振込, 送金, 转账, 송금, भुगतान, تحويل, העברה, โอน, … or any
                    Latin context word above): the run AFTER it (particles such as
                    को / إلى skipped, up to 3 words), and the run BEFORE it only
                    across a recipient particle (山田太郎への振込, राहुल शर्मा को
                    भुगतान, 김민수에게 송금).  A compound without a particle
                    (家賃振込, किराया भुगतान) and a small vocabulary (fees, salary,
                    rent, tax, bank, …) are kept.
    Limits          Heuristic, not NER.  Not detected: names in all lower case
                    ("payment to jordan …"); a single capitalised word after a weak
                    context; names with no context word in a non-cased script
                    ("山田太郎 給与"); names before a context word without a
                    particle; Arabic / Hebrew names written with an attached
                    prefix (لأحمد); a name that is itself a vocabulary word.
                    Over-redaction is accepted instead: unknown capitalised words
                    after a context word (vendor names included) and unknown
                    ALL-CAPS words are tokenised.  Protected entity / partner
                    names (above) are exact and run first.
    Tokens          Stable per session and keyed case-/space-insensitively, so
                    "JÜRGEN MÜLLER" and "Jürgen Müller" share one <PERSON_nnn>.  A
                    detected name is replaced only at word boundaries (never inside
                    a longer cased word); nothing is logged except counts.
    audit_egress()  Does not block heuristic names (too noisy for a hard gate); it
                    blocks protected names (P-04) and account numbers (P-05).
"""
from __future__ import annotations

import bisect
import functools
import json
import logging as _logging
import re
import string
import unicodedata
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

# ── Masked / partial account references (P-05) ────────────────────────────────
# ACCOUNT_MARKER_PATTERN is the ONE marker grammar shared with
# intelligence/owner_draws.py (``account_refs``), so the owner-draw matcher and
# the egress firewall agree on what an account reference is.  Case-insensitive:
#   x1234 / XXXX1234 / XXXX-1234   (x not preceded by a letter or digit)
#   *1234 / **1234 / ** 1234       (any run of stars)
#   ...1234 / …1234
#   acct 1234 / ACCT# 1234 / acct no. 1234 / account 1234 / account ending (in) 1234
#   ending in 1234
# A bare "ending 1234" is NOT a marker ("period ending 2025").
ACCOUNT_MARKER_PATTERN = r"""
    (?:
        (?<![a-z0-9])x+-?
      | \*+[ -]?
      | (?:\.{3,}|…)[ ]?
      | \b(?:acct|account|a/c)
            (?:\s*(?:no\.?|number|\#))?
            (?:\s+ending(?:\s+in)?)?
            [\s:\#-]*
      | \bending\s+in[\s:\#-]*
    )
"""

# owner_draws grammar: exactly 4 digits after a marker, not followed by another
# digit (or "-digit"), so the tail of a longer run, dates and amounts never match.
ACCOUNT_REF_RE = re.compile(
    ACCOUNT_MARKER_PATTERN + r"(\d{4})(?!\d|-\d)",
    re.IGNORECASE | re.VERBOSE,
)

# Privacy-only extra markers (the firewall may be broader than owner_draws,
# never narrower): bullet masks, "card/checking/savings ending 1234",
# "last 4 (digits) 1234", and common non-English account words.
_EXTRA_MARKER_PATTERN = r"""
    (?:
        [•●]+[ ]?
      | \b(?:card|checking|savings|chk|sav)\s+ending[\s:\#-]*
      | \blast[\s-]*(?:4|four)(?:\s+digits)?[\s:\#-]*
      | \b(?:konto|kto|cuenta|cta|compte|conta)\.?
            (?:[\s-]*(?:nr|no|n[º°o])\.?)?
            [\s:\#-]*
    )
"""

# Firewall grammar: any marker followed by 4+ digits (a superset of
# ACCOUNT_REF_RE: every owner_draws match is also matched here).
_MASKED_REF_RE = re.compile(
    r"(?:" + ACCOUNT_MARKER_PATTERN + r"|" + _EXTRA_MARKER_PATTERN + r")"
    r"(\d{4,})(?!\d|-\d)",
    re.IGNORECASE | re.VERBOSE,
)

# ── Bare digit runs (P-05) ────────────────────────────────────────────────────
# Either 8+ consecutive digits, or 3–6-digit groups joined by ONE consistent
# separator (single space or single hyphen).  Filters in _bare_digit_runs().
_DIGIT_RUN_RE = re.compile(
    r"(?<!\d)"
    r"(?:\d{3,6}(?P<sep>[ -])\d{3,6}(?:(?P=sep)\d{3,6}){0,4}"
    r"|\d{8,})"
    r"(?!\d)"
)
_CURRENCY_CHARS = "$€£¥₹"
_HEX_ID_CHARS = frozenset(string.hexdigits + "-")

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

# Person / counterparty name heuristics (P-07) live next to their detectors
# below ("Unicode-aware name heuristics").


# ── Per-session pseudonym registry ────────────────────────────────────────────
# Maps (category, original_value) → token.
# NEVER persisted; regenerated each process invocation.

_session_map: dict[tuple[str, str], str] = {}
_session_counters: dict[str, int] = {}


def _reset_session() -> None:
    """Reset per-session pseudonym state.  Call from tests between cases."""
    _session_map.clear()
    _session_counters.clear()


def _stable_token(category: str, original: str) -> str:
    """
    Return a stable-per-session token ``<CATEGORY_nnn>`` for (category, original).
    Within one process invocation, the same (category, original) always
    produces the same token — stable pseudonyms for coherent AI reasoning.
    The token carries only a per-session counter, never any part of the
    original (P-05: no last-4 of an account or card inside a token).
    """
    key = (category, original)
    if key not in _session_map:
        n = _session_counters.get(category, 0) + 1
        _session_counters[category] = n
        _session_map[key] = f"<{category}_{n:03d}>"
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
            hits.append((raw, _stable_token("CARD", digits)))
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
            hits.append((orig, _stable_token("ACCT", orig)))
    return hits


def _detect_masked_accounts(text: str) -> list[tuple[str, str]]:
    """Marker-anchored partial / masked references (``x1234``, ``*1234``, …).

    The whole match (marker + digits) is replaced.  The token key is the digits,
    so ``x1234`` and ``acct 1234`` share one token, and a full number found
    here or by _detect_account_numbers / _detect_digit_runs gets the same token.
    """
    return [
        (m.group(0), _stable_token("ACCT", m.group(1)))
        for m in _MASKED_REF_RE.finditer(text)
    ]


def _is_date8(d: str) -> bool:
    """True if an 8-digit string is a valid YYYYMMDD, MMDDYYYY or DDMMYYYY date."""
    import datetime as _dt
    for y, m, dd in ((d[:4], d[4:6], d[6:]), (d[4:], d[:2], d[2:4]), (d[4:], d[2:4], d[:2])):
        try:
            year = int(y)
            if 1900 <= year <= 2099:
                _dt.date(year, int(m), int(dd))
                return True
        except ValueError:
            continue
    return False


def _is_compact_timestamp(d: str) -> bool:
    """True for YYYYMMDDHHMM or YYYYMMDDHHMMSS with a valid date and time."""
    if len(d) not in (12, 14):
        return False
    import datetime as _dt
    try:
        year = int(d[:4])
        if not 1900 <= year <= 2099:
            return False
        _dt.datetime(year, int(d[4:6]), int(d[6:8]), int(d[8:10]), int(d[10:12]),
                     int(d[12:14]) if len(d) == 14 else 0)
        return True
    except ValueError:
        return False


def _in_hex_identifier(text: str, start: int, end: int) -> bool:
    """True when the digits sit inside a hex id / UUID / digest (≥16 hex chars)."""
    lo, hi = start, end
    while lo > 0 and text[lo - 1] in _HEX_ID_CHARS:
        lo -= 1
    while hi < len(text) and text[hi] in _HEX_ID_CHARS:
        hi += 1
    if (lo > 0 and text[lo - 1].isalnum()) or (hi < len(text) and text[hi].isalnum()):
        return False  # part of a longer alphanumeric word, not a pure hex id
    token = text[lo:hi]
    compact = token.replace("-", "")
    return len(compact) >= 16 and any(c in "abcdefABCDEF" for c in compact)


def _bare_digit_runs(text: str) -> list[tuple[str, str]]:
    """(raw match, digits) for every bare 8+ digit run that is not excluded.

    Exclusions (see module docstring "Account numbers"): decimal amounts and
    currency-prefixed amounts, 8-digit dates, compact timestamps, two
    space-separated numbers, and digits inside a hex id / UUID / digest.
    """
    out: list[tuple[str, str]] = []
    for m in _DIGIT_RUN_RE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 8:
            continue
        s, e = m.span()
        sep = m.group("sep")
        if sep == " " and raw.count(" ") < 2:
            continue  # "2026 1500" — two values, not one grouped number
        if (len(digits) <= 10 and sep is None and e + 1 < len(text)
                and text[e] in ".," and text[e + 1].isdigit()):
            continue  # 12345678.90 — decimal amount (≤10 integer digits)
        if s >= 2 and text[s - 1] in ".," and text[s - 2].isdigit():
            continue  # fractional part of an amount
        before = text[max(0, s - 2):s].strip()
        if before and before[-1] in _CURRENCY_CHARS:
            continue  # $12345678 — amount
        if len(digits) == 8 and _is_date8(digits):
            continue  # 20260115 / 01152026 / 2026-0115
        if _is_compact_timestamp(digits):
            continue  # YYYYMMDDHHMM[SS]
        if sep != " " and _in_hex_identifier(text, s, e):
            continue  # transaction id / UUID / sha digest
        out.append((raw, digits))
    return out


def _detect_digit_runs(text: str) -> list[tuple[str, str]]:
    """Bare 8+ digit runs (optionally grouped) without an account keyword."""
    return [(raw, _stable_token("ACCT", digits)) for raw, digits in _bare_digit_runs(text)]


def contains_account_reference(text: str) -> str | None:
    """Category of the first account-number leak in ``text``, or None.

    ``"account_reference"`` for a marker + 4-digit form, ``"digit_run"`` for a
    bare 8+ digit run.  Never returns the matched value.
    """
    if _MASKED_REF_RE.search(text):
        return "account_reference"
    if _bare_digit_runs(text):
        return "digit_run"
    return None


# ── Opaque error refs (P-15) ──────────────────────────────────────────────────

_REF_LETTERS = "abcdef"


def new_error_ref() -> str:
    """Fresh opaque id for an error message: 8 lowercase hex chars, >= 1 letter.

    The wire format stays ``ref:XXXXXXXX`` ([0-9a-f]{8}).  An all-digit ref
    would be tokenised as a bare digit run by the ``log`` scope and could no
    longer be matched between the client message and stderr, so refs that come
    out all digits are drawn again (uniform over the letter-bearing hex ids,
    about 32 bits).  Shared by the bridge and both MCP servers.
    """
    import secrets

    for _ in range(16):
        ref = secrets.token_hex(4)
        if any(c in _REF_LETTERS for c in ref):
            return ref
    # Unreachable in practice ((10/16)**128); keeps the letter guarantee if
    # ``secrets`` is ever stubbed to return digits only.
    return secrets.choice(_REF_LETTERS) + ref[1:]


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


# ── Unicode-aware name heuristics (P-07) ─────────────────────────────────────
# Three detectors replace the old ASCII-only ones (see the module docstring,
# "Person and counterparty names", for the heuristics and their limits):
#   • _detect_counterparty_names — ALL-CAPS runs in any cased script
#   • _detect_person_names       — names after a payee / transfer context word,
#                                  cased scripts (Title Case or ALL CAPS) and
#                                  non-cased scripts (CJK, kana, Devanagari, …)
# Every word is classified on its own (_classify_words): an allowlisted vendor,
# a banking/system word or a vocabulary word is exempt BY ITSELF; its
# neighbours are still evaluated.

# Company-name suffixes: never a name on their own, and they turn a person
# candidate into a counterparty.
_COMPANY_SUFFIXES: set[str] = {
    "LLC", "INC", "CORP", "LTD", "DBA", "CO", "LP", "LLP", "PC", "PA",
    "PLLC", "PLC", "NV", "SA", "AG", "GMBH", "SARL", "SRL", "BV", "KG", "OHG",
    "SL", "SAS", "LTDA", "OOO", "ООО", "АО", "ПАО",
}

# Name particles: allowed between name words ("José de la Peña",
# "Anna von Beispiel"), trimmed from the edges of a detected name.
_NAME_PARTICLES: frozenset[str] = frozenset({
    "de", "da", "do", "dos", "das", "del", "della", "di", "du", "des", "la", "le",
    "van", "von", "der", "den", "ten", "ter", "y", "e", "bin", "ibn", "al", "el",
})

# Generic words that are never a name (folded with str.casefold()).  Short,
# documented, no real names.  English banking / ACH field words first, then
# the payment & bookkeeping vocabulary of the languages the context words cover.
_GENERIC_WORDS: frozenset[str] = frozenset(w.casefold() for w in (
    # English connectors (also split ALL-CAPS runs)
    "TO", "FROM", "FOR", "OF", "THE", "AND", "ON", "AT", "IN", "BY", "VIA", "WITH",
    "PER", "OR", "NO", "RE", "A", "AN", "IS", "YOUR", "OUR", "MY",
    # English banking / ACH vocabulary
    "ACCOUNT", "ACCOUNTS", "ACCT", "CHECKING", "SAVINGS", "SAV", "BUSINESS", "CARD",
    "ONLINE", "MOBILE", "BANKING", "AUTOPAY", "AUTO", "RECURRING", "BILL", "BILLPAY",
    "PAYMENTS", "PMTS", "PYMT", "PAYMT", "XFER", "TRANSFERS", "INSTANT", "SAME", "DAY",
    "ORIG", "DES", "INDN", "ID", "ENTRY", "DESCR", "SEC", "TRACE", "CONF",
    "CONFIRMATION", "NUMBER", "ORDER", "INVOICE", "INV", "MERCHANT", "MKTPLACE",
    "MARKETPLACE", "STORE", "SHOP", "SUBSCRIPTION", "MONTHLY", "ANNUAL", "PLAN",
    "SERVICE", "SVC", "SVCS", "SUPPLIES", "OFFICE", "TAX", "TAXES", "LOAN", "MORTGAGE",
    "RENT", "SALARY", "DIRECT", "CHECK", "CHEQUE", "CASH", "VISA", "MASTERCARD",
    "AMEX", "DISCOVER", "RETURN", "RETURNED", "REVERSAL", "ADJUSTMENT", "REINVEST",
    "BUY", "SELL", "SOLD", "BOUGHT", "SHARES", "THANK", "YOU", "RECEIVED", "SENT",
    "INTL", "DOMESTIC", "OUTGOING", "INCOMING", "INBOUND", "OUTBOUND", "REQUEST",
    "PREAUTHORIZED", "AUTHORIZED", "AUTH", "RECUR", "PURCH", "SELF", "OWN", "BANK",
    "STATEMENT", "PERIOD", "TOTAL", "AMOUNT", "DATE", "MEMO", "NOTE", "REFERENCE",
    "MISC", "OTHER", "NEW", "OPENING", "CLOSING", "ENDING", "BEGINNING", "PAID",
    "PREMIUM", "PLUS", "PRO", "BASIC", "MEMBERSHIP", "RENEWAL", "DIGITAL",
    # German
    "ZAHLUNG", "ÜBERWEISUNG", "UEBERWEISUNG", "LASTSCHRIFT", "GUTSCHRIFT",
    "DAUERAUFTRAG", "KARTENZAHLUNG", "ABBUCHUNG", "EINZAHLUNG", "AUSZAHLUNG",
    "MIETE", "GEHALT", "LOHN", "STEUER", "STEUERN", "FINANZAMT", "GEBÜHR", "GEBÜHREN",
    "ENTGELT", "ZINSEN", "VERSICHERUNG", "BÜRO", "BÜROBEDARF", "BÜROMATERIAL",
    "RECHNUNG", "KONTO", "KONTOFÜHRUNG", "STROM", "TELEFON", "KRANKENKASSE",
    "BEITRAG", "BARGELD", "KARTE", "REFERENZ", "VERWENDUNGSZWECK", "MANDAT",
    "GLÄUBIGER", "UND", "FÜR", "AN", "ZU", "ZUM", "ZUR", "BEI", "MIT", "IM",
    # Spanish
    "PAGO", "TRANSFERENCIA", "RECIBO", "FACTURA", "CUENTA", "NÓMINA", "NOMINA",
    "ALQUILER", "IMPUESTO", "IMPUESTOS", "COMISIÓN", "COMISION", "SEGURO", "TARJETA",
    "COMPRA", "DEPÓSITO", "DEPOSITO", "RETIRO", "CAJERO", "INTERESES", "CUOTA",
    "SUSCRIPCIÓN", "SUSCRIPCION", "MENSUAL", "DOMICILIACIÓN", "ADEUDO", "ABONO",
    "CARGO", "PARA", "POR", "CON", "EN",
    # French
    "PAIEMENT", "VIREMENT", "PRÉLÈVEMENT", "PRELEVEMENT", "FACTURE", "COMPTE",
    "LOYER", "SALAIRE", "IMPÔT", "IMPÔTS", "IMPOTS", "FRAIS", "ASSURANCE", "CARTE",
    "ACHAT", "RETRAIT", "DÉPÔT", "COTISATION", "ABONNEMENT", "MENSUEL", "INTÉRÊTS",
    "REMBOURSEMENT", "CHÈQUE", "POUR", "AVEC", "SUR", "PAR", "ET", "À", "AU", "AUX",
    # Portuguese
    "PAGAMENTO", "TRANSFERÊNCIA", "BOLETO", "FATURA", "CONTA", "ALUGUEL", "SALÁRIO",
    "SALARIO", "IMPOSTO", "TAXA", "TARIFA", "CARTÃO", "CARTAO", "SAQUE", "JUROS",
    "ASSINATURA", "MENSALIDADE", "PIX", "TED", "COM", "EM",
    # Russian
    "ПЕРЕВОД", "ОПЛАТА", "ПЛАТЕЖ", "ПЛАТЁЖ", "СЧЕТ", "СЧЁТ", "КАРТА", "БАНК",
    "КОМИССИЯ", "ЗАРПЛАТА", "НАЛОГ", "АРЕНДА", "ПОКУПКА", "СНЯТИЕ", "ВЗНОС", "ПЛАТА",
    "ДЛЯ", "ОТ", "ЗА", "НА", "В", "К", "И", "С", "ПО",
))

# ── Context words ─────────────────────────────────────────────────────────────
# Strong context — one name word is enough:
#   • a payment noun, optionally one more word, then a payee preposition
#     ("Zelle payment to", "Zahlung an", "Pago a", "Virement à", "Pix para",
#     "Перевод для");
#   • a payee label ("Payee", "Empfänger", "Beneficiario", "Bénéficiaire", …).
# Weak context — two name words are needed: bare English "to" / "from" and the
# personnel / honorific words (Customer, Mr, Herr, Sra, …).
_PAYMENT_NOUNS = (
    # en
    "PAYMENT", "PAYMENTS", "PMT", "PAY", "PAID", "TRANSFER", "XFER", "ZELLE", "VENMO",
    "WIRE", "SENT", "RECEIVED", "DEPOSIT", "REFUND", "REIMBURSEMENT", "CREDIT", "DEBIT",
    # de
    "ZAHLUNG", "ÜBERWEISUNG", "UEBERWEISUNG", "GUTSCHRIFT", "LASTSCHRIFT",
    "DAUERAUFTRAG", "EINZAHLUNG",
    # es
    "PAGO", "TRANSFERENCIA", "ABONO", "BIZUM", "DEPÓSITO", "DEPOSITO",
    # fr
    "PAIEMENT", "VIREMENT", "PRÉLÈVEMENT", "PRELEVEMENT", "REMBOURSEMENT",
    # pt
    "PAGAMENTO", "TRANSFERÊNCIA", "PIX", "TED",
    # ru
    "ПЕРЕВОД", "ОПЛАТА", "ПЛАТЕЖ", "ПЛАТЁЖ",
)
_PAYEE_PREPOSITIONS = (
    "TO", "FROM",                    # en
    "AN", "VON", "FÜR",              # de
    "A", "DE", "PARA",               # es / pt
    "À", "POUR",                     # fr
    "ДЛЯ", "ОТ",                     # ru
)
_PAYEE_LABELS = (
    "PAY TO THE ORDER OF", "PAYEE", "BENEFICIARY", "BENE", "REMITTER", "ORIGINATOR",
    "EMPFÄNGER", "BEGÜNSTIGTER", "AUFTRAGGEBER",
    "BENEFICIARIO", "ORDENANTE", "BÉNÉFICIAIRE", "BENEFICIAIRE",
    "FAVORECIDO", "BENEFICIÁRIO", "ПОЛУЧАТЕЛЬ",
)
_WEAK_CONTEXTS = (
    "TO", "FROM",
    "CUSTOMER", "CLIENT", "MEMBER", "PARTNER NAME", "PARTNER", "OWNER", "EMPLOYEE",
    "CONTRACTOR", "AUTHORIZED SIGNER", "SIGNER", "PREPARED BY", "FILED BY",
    "MR", "MRS", "MS", "MISS", "DR", "PROF", "HON", "FULL NAME", "NAME", "TAXPAYER",
    "HERR", "FRAU", "SR", "SRA", "MME",
)


def _alt(words: tuple[str, ...]) -> str:
    ordered = sorted(set(words), key=len, reverse=True)
    return "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in ordered)


_NAME_CTX_RE = re.compile(
    r"(?<!\w)(?:"
    r"(?P<strong>" + _alt(_PAYEE_LABELS) + r"|(?:" + _alt(_PAYMENT_NOUNS)
    + r")(?:\s+[^\s<>]+)?\s+(?:" + _alt(_PAYEE_PREPOSITIONS) + r"))"
    r"|(?P<weak>" + _alt(_WEAK_CONTEXTS) + r")"
    r")(?!\w)",
    re.IGNORECASE,
)

# Gap between the context word and the name, and between two name words.
_CTX_GAP_RE = re.compile(r"[ \t]*[:.,#\-]?[ \t]*")
_NAME_GAP_RE = re.compile(r"[ \t]{1,3}|[\-'’]")
_INITIAL_GAP_RE = re.compile(r"\.[ \t]{0,2}")
_RUN_GAP_RE = re.compile(r"[ \t]{1,3}|[ \t]?[&\-'’][ \t]?")

# ── Non-cased scripts (CJK, kana, Hangul, Devanagari, Arabic, Hebrew, Thai) ──
# No capital letters, so a name is found only next to a payment / transfer
# word.  The name AFTER the context word is always taken (up to 3 words, e.g.
# "振込 ヤマダ タロウ", "भुगतान राहुल शर्मा"); the name BEFORE it only when a
# recipient particle sits in between ("山田太郎への振込", "राहुल शर्मा को भुगतान",
# "김민수에게 송금"), so a plain compound ("家賃振込", "किराया भुगतान") is kept.
_UNCASED_CONTEXTS: tuple[str, ...] = (
    # ja
    "振込先", "振込", "振替", "送金", "支払先", "支払い", "支払", "入金", "受取人", "宛先",
    # zh
    "转账给", "转账", "轉帳", "汇款", "匯款", "付款给", "付款", "收款人", "支付给", "支付",
    # ko
    "송금", "이체", "입금", "수취인", "받는분",
    # hi
    "भुगतान", "स्थानांतरण", "हस्तांतरण", "प्राप्तकर्ता", "भेजा",
    # ar
    "تحويل", "حوالة", "دفع", "المستفيد", "لصالح",
    # he
    "העברה", "תשלום", "מוטב",
    # th
    "โอนเงิน", "โอน", "ชำระ", "ผู้รับ",
)
# Particles written attached to the name (ja / zh / ko / th) and as separate
# words (hi / ar / he).  "の" is deliberately absent (possessive, too common).
_ATTACHED_PARTICLES: tuple[str, ...] = tuple(sorted((
    "への", "へ", "に", "から", "より", "宛て", "宛", "様", "さま", "殿",
    "给", "給", "向", "从", "從",
    "에게서", "에게", "한테", "님", "께",
    "ให้", "จาก", "แก่",
), key=len, reverse=True))
_SPACED_PARTICLES: tuple[str, ...] = tuple(sorted((
    "के लिए", "को", "से", "के", "का", "की", "द्वारा",
    "إلى", "الى", "من",
    "אל", "מאת",
), key=len, reverse=True))
# Generic non-cased words that are never a name (fees, salary, rent, …).
_UNCASED_VOCAB: frozenset[str] = frozenset((
    "手数料", "給与", "給料", "家賃", "電気", "電気料金", "料金", "税金", "口座", "銀行",
    "利息", "代金", "会費", "振込手数料",
    "手续费", "工资", "房租", "电费", "税", "账户", "银行",
    "수수료", "급여", "월세", "세금", "계좌", "은행", "이자",
    "किराया", "वेतन", "बिजली", "बिल", "शुल्क", "कर", "खाता", "बैंक", "ब्याज", "ऋण",
    "रसीद", "ऑनलाइन",
    "رسوم", "راتب", "إيجار", "فاتورة", "حساب", "بنك", "ضريبة",
    "עמלה", "משכורת", "שכירות", "חשבון", "בנק",
    "ค่าธรรมเนียม", "เงินเดือน", "ค่าเช่า", "บัญชี", "ธนาคาร", "เงิน",
))
_UNCASED_CTX_RE = re.compile(_alt(_UNCASED_CONTEXTS))
_UNCASED_JOINERS = frozenset("・·")


def _cat(ch: str) -> str:
    return unicodedata.category(ch)


def _is_cased_letter(ch: str) -> bool:
    return ch.isupper() or ch.islower() or _cat(ch) == "Lt"


def _is_uncased_letter(ch: str) -> bool:
    """A letter with no case (CJK, kana, Hangul, Devanagari, Arabic, …) or a mark."""
    c = _cat(ch)
    return c in ("Lo", "Lm") or c[0] == "M"


def _fold(word: str) -> str:
    return unicodedata.normalize("NFC", word).casefold()


def _word_spans(text: str) -> list[tuple[int, int]]:
    """Maximal letter (+ combining mark) runs; a run starts with a letter."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if _cat(text[i])[0] == "L":
            j = i + 1
            while j < n and _cat(text[j])[0] in "LM":
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _attached(text: str, s: int, e: int) -> bool:
    """True when the word is glued to a digit or "_" (ENTITY_A, <PERSON_001>, S3)."""
    return ((s > 0 and (text[s - 1].isdigit() or text[s - 1] == "_"))
            or (e < len(text) and (text[e].isdigit() or text[e] == "_")))


def _is_upper_word(word: str) -> bool:
    """ALL CAPS in any cased script ("MÜLLER", "ПЕТРОВ"); ß counts as caps."""
    return (any(c.isupper() for c in word)
            and not any(c.islower() and c != "ß" for c in word))


def _starts_upper(word: str) -> bool:
    return word[0].isupper() or _cat(word[0]) == "Lt"


# ── Vocabulary that is never redacted as a name ───────────────────────────────

_vocab_cache: tuple[frozenset[str], frozenset[tuple[str, ...]], int] | None = None
_vocab_building = False


def _phrase_tokens(entry: str) -> tuple[str, ...]:
    return tuple(_fold(entry[s:e]) for s, e in _word_spans(entry))


def _coa_vocabulary() -> set[str]:
    """Words of the default chart of accounts (names + keywords).

    Keywords of the securities rows are skipped: they are holdings, not
    bookkeeping vocabulary.
    """
    from ledger_agent.core.database import _DEFAULT_COA
    words: set[str] = set()
    for row in _DEFAULT_COA:
        name, keywords = str(row[1]), str(row[5] or "[]")
        words.update(_phrase_tokens(name))
        if "securities" in name.casefold():
            continue
        for kw in json.loads(keywords):
            words.update(_phrase_tokens(str(kw)))
    return words


def _local_vendor_vocabulary() -> set[str]:
    """Upper-case words (3+ letters) of the local classifier's keyword rules.

    Covers the fuzzy vendor list too (its keys all appear in the rules).
    """
    from ledger_agent.core.intelligence.ai_backend.local_backend import _RULES
    words: set[str] = set()
    for pattern, *_rest in _RULES:
        words.update(w.casefold() for w in re.findall(r"[A-Z]{3,}", pattern))
    return words


def _name_vocab() -> tuple[frozenset[str], frozenset[tuple[str, ...]], int]:
    """(single exempt words, multi-word allowlist phrases, longest phrase).

    Single words: allowlist one-word entries, _SYSTEM_WORDS, _GENERIC_WORDS,
    COA vocabulary and the local classifier's vendor words.  Multi-word
    allowlist entries ("Wells Fargo", "Cash App") are exempt only as a phrase.
    Cached once the COA and vendor lists loaded (lazy imports: privacy is
    imported by the modules it reads, so a failed import falls back to the
    static lists and is retried on the next call).
    """
    global _vocab_cache, _vocab_building
    if _vocab_cache is not None:
        return _vocab_cache
    single: set[str] = set(_GENERIC_WORDS)
    single.update(w.casefold() for w in _SYSTEM_WORDS)
    for ctx in (*_PAYMENT_NOUNS, *_PAYEE_PREPOSITIONS, *_PAYEE_LABELS, *_WEAK_CONTEXTS):
        single.update(_phrase_tokens(ctx))
    phrases: set[tuple[str, ...]] = set()
    for entry in _load_allowlist():
        toks = _phrase_tokens(entry)
        if len(toks) == 1:
            single.add(toks[0])
        elif toks:
            phrases.add(toks)
    complete = False
    if not _vocab_building:
        _vocab_building = True
        try:
            single |= _coa_vocabulary()
            single |= _local_vendor_vocabulary()
            complete = True
        except Exception as exc:  # noqa: BLE001 — static lists still apply
            _logging.getLogger("fi.privacy").debug(
                "name vocabulary: optional source unavailable (%s)", type(exc).__name__
            )
        finally:
            _vocab_building = False
    result = (frozenset(single), frozenset(phrases), max((len(p) for p in phrases), default=1))
    if complete:
        _vocab_cache = result
    return result


def _classify_words(words: list[str]) -> list[str]:
    """Per-word class: "exempt" | "particle" | "neutral" | "name".

    exempt   allowlist (single word, or every word of a matched multi-word
             entry), system / generic / COA / vendor vocabulary.  Never
             redacted; splits a run — its neighbours are evaluated on their own.
    particle name particle (de, von, van, …): kept inside a name, trimmed at edges.
    neutral  single letters, company suffixes, generic business words: part of
             a name, but never a name on their own.
    name     anything else.
    """
    single, phrases, max_len = _name_vocab()
    folded = [_fold(w) for w in words]
    classes = [""] * len(words)
    i = 0
    while i < len(words):
        step = 1
        for size in range(min(max_len, len(words) - i), 1, -1):
            if tuple(folded[i:i + size]) in phrases:
                for k in range(i, i + size):
                    classes[k] = "exempt"
                step = size
                break
        i += step
    for k, f in enumerate(folded):
        if classes[k]:
            continue
        up = f.upper()
        if f in _NAME_PARTICLES:
            classes[k] = "particle"
        elif f in single:
            classes[k] = "exempt"
        elif len(f) == 1 or up in _COMPANY_SUFFIXES or up in _NAME_STOPWORDS:
            classes[k] = "neutral"
        else:
            classes[k] = "name"
    return classes


def _trim_particles(items: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    while items and items[0][2] == "particle":
        items = items[1:]
    while items and items[-1][2] == "particle":
        items = items[:-1]
    return items


def _name_key(span: str) -> str:
    """Session key of a detected name: whitespace-collapsed and case-folded, so
    "JÜRGEN MÜLLER" and "Jürgen Müller" share one token."""
    return " ".join(_fold(span).split())


# ── Counterparty: ALL-CAPS runs in any cased script ──────────────────────────

def _detect_counterparty_names(text: str) -> list[tuple[str, str]]:
    """ALL-CAPS word runs (Latin, Cyrillic, Greek, …) that are not vocabulary.

    A run is split at every exempt word; each remaining sub-run is a
    counterparty when it holds a "name" word and is long enough: 5+ characters
    for a run that was not split (unchanged from before P-07, keeps short
    acronyms), 3+ name letters for a piece split off by an exempt word
    ("ZELLE PAYMENT TO JORDAN SAMPLEPERSON" → the name only).
    """
    spans = _word_spans(text)
    runs: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    for s, e in spans:
        if _is_upper_word(text[s:e]) and not _attached(text, s, e):
            if cur and _RUN_GAP_RE.fullmatch(text[cur[-1][1]:s]):
                cur.append((s, e))
                continue
            if cur:
                runs.append(cur)
            cur = [(s, e)]
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for run in runs:
        classes = _classify_words([text[s:e] for s, e in run])
        pieces: list[list[tuple[int, int, str]]] = [[]]
        for (s, e), cls in zip(run, classes):
            if cls == "exempt":
                pieces.append([])
            else:
                pieces[-1].append((s, e, cls))
        split = "exempt" in classes
        for piece in pieces:
            piece = _trim_particles(piece)
            if not any(cls == "name" for _s, _e, cls in piece):
                continue
            orig = text[piece[0][0]:piece[-1][1]]
            if split:
                if sum(e - s for s, e, cls in piece if cls == "name") < 3:
                    continue
            elif len(orig) < 5:
                continue
            if orig in seen:
                continue
            seen.add(orig)
            hits.append((orig, _stable_token("COUNTERPARTY", _name_key(orig))))
    return hits


# ── Person: names after a payee / transfer context word ──────────────────────

def _cased_name_after(text: str, spans: list[tuple[int, int]], idx: int,
                      strong: bool) -> tuple[str, str] | None:
    """(name span, category) for the cased-script name starting at spans[idx]."""
    raw: list[tuple[int, int]] = []
    prev_end = -1
    for s, e in spans[idx:idx + 8]:
        word = text[s:e]
        if prev_end >= 0:
            gap = text[prev_end:s]
            initial = raw[-1][1] - raw[-1][0] == 1
            if not (_NAME_GAP_RE.fullmatch(gap)
                    or (initial and _INITIAL_GAP_RE.fullmatch(gap))):
                break
        if _attached(text, s, e) or not _is_cased_letter(word[0]):
            break
        if not _starts_upper(word) and _fold(word) not in _NAME_PARTICLES:
            break
        raw.append((s, e))
        prev_end = e
    if not raw:
        return None
    classes = _classify_words([text[s:e] for s, e in raw])
    for k, (s, e) in enumerate(raw):
        if e - s == 1 and e < len(text) and text[e] == "." and _starts_upper(text[s]):
            classes[k] = "neutral"    # middle initial ("John A. Smith")
    picked: list[tuple[int, int, str]] = []
    after: str | None = None
    skipped = 0
    for (s, e), cls in zip(raw, classes):
        if cls == "exempt":
            if not picked and skipped < 2:
                skipped += 1      # leading vocabulary word ("to Zelle Jordan …")
                continue
            after = text[s:e]
            break
        if cls == "particle" and not picked:
            continue
        if not _starts_upper(text[s:e]) and cls != "particle":
            break
        picked.append((s, e, cls))
        if sum(1 for *_x, c in picked if c in ("name", "neutral")) >= 4:
            break
    picked = _trim_particles(picked)
    names = sum(1 for *_x, c in picked if c == "name")
    if names < (1 if strong else 2):
        return None
    orig = text[picked[0][0]:picked[-1][1]]
    words = {text[s:e].upper() for s, e, _c in picked}
    if after is not None:
        words.add(after.upper())
    category = "COUNTERPARTY" if words & _COMPANY_SUFFIXES else "PERSON"
    return orig, category


def _match_particle_at(text: str, pos: int) -> int:
    """Length of a particle starting at pos (0 when none)."""
    for p in _ATTACHED_PARTICLES:
        if text.startswith(p, pos):
            return len(p)
    for p in _SPACED_PARTICLES:
        end = pos + len(p)
        if text.startswith(p, pos) and (end == len(text) or not _is_word_char(text[end])):
            return len(p)
    return 0


def _particle_before(text: str, pos: int) -> int:
    """Length of a particle ending at pos (0 when none)."""
    for p in _ATTACHED_PARTICLES:
        if pos >= len(p) and text.startswith(p, pos - len(p)):
            return len(p)
    for p in _SPACED_PARTICLES:
        start = pos - len(p)
        if (start >= 0 and text.startswith(p, start)
                and (start == 0 or not _is_word_char(text[start - 1]))):
            return len(p)
    return 0


def _uncased_core_ok(core: str) -> bool:
    return (sum(1 for c in core if _cat(c)[0] == "L") >= 2
            and core not in _UNCASED_VOCAB
            and not _UNCASED_CTX_RE.fullmatch(core)
            and core not in _ATTACHED_PARTICLES and core not in _SPACED_PARTICLES)


def _uncased_end(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and (_is_uncased_letter(text[pos]) or text[pos] in _UNCASED_JOINERS):
        pos += 1
    return pos


def _uncased_start(text: str, pos: int) -> int:
    while pos > 0 and (_is_uncased_letter(text[pos - 1]) or text[pos - 1] in _UNCASED_JOINERS):
        pos -= 1
    return pos


def _skip_spaces(text: str, pos: int, step: int, limit: int = 3) -> int | None:
    """Move over up to ``limit`` spaces/tabs (forward or backward); None if longer."""
    k = pos
    if step > 0:
        while k < len(text) and text[k] in " \t":
            k += 1
    else:
        while k > 0 and text[k - 1] in " \t":
            k -= 1
    return k if abs(k - pos) <= limit else None


def _uncased_name_after(text: str, pos: int) -> tuple[int, int] | None:
    """Name in a non-cased script right after a context word (up to 3 words)."""
    k = _CTX_GAP_RE.match(text, pos)
    pos = k.end() if k else pos
    for _ in range(2):                      # e.g. "भुगतान को", "送金 様"
        plen = _match_particle_at(text, pos)
        if not plen:
            break
        nxt = _skip_spaces(text, pos + plen, 1)
        if nxt is None:
            return None
        pos = nxt
    words: list[tuple[int, int]] = []
    while len(words) < 3 and pos < len(text) and _is_uncased_letter(text[pos]) \
            and _cat(text[pos])[0] == "L":
        end = _uncased_end(text, pos)
        core_end = end
        inner = _UNCASED_CTX_RE.search(text, pos + 1, end)
        if inner:
            core_end = inner.start()
        for _ in range(2):
            plen = _particle_before(text[:core_end], core_end)
            if plen and core_end - plen > pos:
                core_end -= plen
            else:
                break
        core = text[pos:core_end]
        if not _uncased_core_ok(core):
            break
        words.append((pos, core_end))
        if core_end < end:
            break
        nxt = _skip_spaces(text, end, 1)
        if nxt is None or nxt == end:
            break
        pos = nxt
    return (words[0][0], words[-1][1]) if words else None


def _uncased_name_before(text: str, pos: int) -> tuple[int, int] | None:
    """Name in a non-cased script before a context word, only across a particle."""
    i = _skip_spaces(text, pos, -1)
    if i is None:
        return None
    found = False
    for _ in range(2):                      # "山田太郎様への振込"
        plen = _particle_before(text, i)
        if not plen:
            break
        found = True
        nxt = _skip_spaces(text, i - plen, -1)
        if nxt is None:
            return None
        i = nxt
    if not found:
        return None
    words: list[tuple[int, int]] = []
    while len(words) < 3 and i > 0 and _is_uncased_letter(text[i - 1]):
        start = _uncased_start(text, i)
        last_ctx = None
        for m in _UNCASED_CTX_RE.finditer(text, start, i):
            last_ctx = m
        if last_ctx is not None:
            start = last_ctx.end()
        core = text[start:i]
        if not core or _cat(core[0])[0] != "L" or not _uncased_core_ok(core):
            break
        words.insert(0, (start, i))
        if last_ctx is not None:
            break
        nxt = _skip_spaces(text, start, -1)
        if nxt is None or nxt == start:
            break
        i = nxt
    return (words[0][0], words[-1][1]) if words else None


def _ctx_boundary_ok(text: str, s: int, e: int) -> bool:
    """Spaced scripts need word boundaries around a context word; CJK/kana/Thai don't."""
    if _is_no_space_script(text[s]):
        return True
    return ((s == 0 or not _is_word_char(text[s - 1]))
            and (e == len(text) or not _is_word_char(text[e])))


def _detect_person_names(text: str) -> list[tuple[str, str]]:
    """Context-gated person names in any script (P-07).

    Cased scripts: after a strong context one name word is enough, after a
    weak one two are needed (see _NAME_CTX_RE).  Non-cased scripts: the word
    run next to a payment / transfer word (_UNCASED_CONTEXTS) or after any
    Latin context word.
    """
    hits: list[tuple[str, str]] = []
    spans = _word_spans(text)
    starts = [s for s, _e in spans]
    found: list[tuple[int, int]] = []

    for m in _NAME_CTX_RE.finditer(text):
        gap = _CTX_GAP_RE.match(text, m.end())
        pos = gap.end() if gap else m.end()
        if "\n" in text[m.end():pos]:
            continue
        if pos < len(text) and _is_uncased_letter(text[pos]) and _cat(text[pos])[0] == "L":
            span = _uncased_name_after(text, pos)
            if span:
                found.append(span)
            continue
        idx = bisect.bisect_left(starts, pos)
        if idx >= len(spans) or spans[idx][0] != pos:
            continue
        res = _cased_name_after(text, spans, idx, strong=m.group("strong") is not None)
        if res:
            orig, category = res
            hits.append((orig, _stable_token(category, _name_key(orig))))

    for m in _UNCASED_CTX_RE.finditer(text):
        if not _ctx_boundary_ok(text, m.start(), m.end()):
            continue
        for span in (_uncased_name_after(text, m.end()),
                     _uncased_name_before(text, m.start())):
            if span:
                found.append(span)

    for s, e in found:
        orig = text[s:e]
        hits.append((orig, _stable_token("PERSON", _name_key(orig))))
    return hits


def _name_hit_bounded(text: str, s: int, e: int) -> bool:
    """Boundary rule for replacing a detected name: a cased / digit edge must not
    continue into another cased letter or digit ("ANA" never inside "BANANA");
    an edge in a non-cased script needs no boundary (no spaces between words)."""
    def glued(a: str, b: str) -> bool:
        return (a.isdigit() or _is_cased_letter(a)) and (b.isdigit() or _is_cased_letter(b)
                                                          or b == "_")
    if s > 0 and glued(text[s], text[s - 1]):
        return False
    return not (e < len(text) and glued(text[e - 1], text[e]))


_NAME_TOKEN_PREFIXES = ("<PERSON_", "<COUNTERPARTY_")


def _replace_name(text: str, orig: str, token: str) -> str:
    out: list[str] = []
    pos = 0
    for m in re.finditer(re.escape(orig), text):
        if not _name_hit_bounded(text, m.start(), m.end()):
            continue
        out.append(text[pos:m.start()])
        out.append(token)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


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
        from ledger_agent.core.config import PRIVACY_ENTITY_NAME
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
    "partial_account", "digit_run",
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
        "account", "partial_account", "digit_run", "iban", "swift", "email", "phone",
        "driver_license", "passport", "dob",
        "url_creds", "entity_name", "person_name",
    ],
    # Log scope tightened: previously SSN/EIN/API-keys only; now mirrors the
    # full structural-identifier set + network identifiers + counterparty names
    # so audit/log lines never leak the substance of a record even if a caller
    # forgets to redact first. Person-name remains context-gated (needs titles
    # like "Mr/Ms/Customer:") to keep false-positive noise tolerable.
    # P-15: partial_account + digit_run added — stderr is inherited by the Java
    # webapp (P-14), so a log line needs the same account firewall as egress.
    "log": [
        "api_key", "ssn", "ein", "itin", "credit_card", "routing", "account",
        "partial_account", "digit_run", "iban", "swift", "email", "phone", "address", "zip4",
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
    from ledger_agent.core.config import AI_EGRESS_MODE, AI_EGRESS_MODE_ACK

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
    if "partial_account" in detectors:
        replacements.extend(_detect_masked_accounts(text))
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
    # Bare digit runs go LAST: on an identical span, a more specific detector
    # (card, SSN, ZIP+4, phone, …) appended earlier wins the dedupe below.
    if "digit_run" in detectors:
        replacements.extend(_detect_digit_runs(text))

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
        if orig not in safe:
            continue
        if token.startswith(_NAME_TOKEN_PREFIXES):
            # Heuristic names (P-07) are replaced only at word boundaries.
            replaced = _replace_name(safe, orig, token)
            if replaced == safe:
                continue
            safe = replaced
        else:
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
    # Masked / partial account references and bare 8+ digit runs (P-05).
    # Scanned on the unescaped copy so "\\uXXXX" escapes cannot fake a digit
    # run.  Category only — never the value.
    try:
        acct_category = contains_account_reference(text_unicode)
    except Exception as exc:  # noqa: BLE001 — fail closed
        raise PrivacyLeakError(
            "audit_egress: account-number check failed. Blocked."
        ) from exc
    if acct_category:
        violations.append(acct_category)
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
    from ledger_agent.core.config import AI_EGRESS_MODE

    return {
        "egress_mode": AI_EGRESS_MODE,
        "detector_categories": len(_ALL_DETECTORS),
        "session_tokens_issued": sum(_session_counters.values()),
        "allowlist_entries": len(_load_allowlist()),
        "recent_egress_events": _egress_event_log[-_MAX_EVENT_LOG:],
    }

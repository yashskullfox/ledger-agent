"""
ledger_agent/core/labels.py  –  account / institution label tokenising (P-12)

One shared tokeniser for every egress path that shows account labels: the legacy
MCP server (``mcp_server/server.py``, P-02) and the AI context export
(``adapters/context_builder.py``, P-06).  Both import it from here, so the same
database state always yields the same tokens on both paths.

Tokens (stable for one DB state, numbered per entity):
  * ``<ACCOUNT_n>``     — the entity's accounts sorted by account id, numbered from 1;
  * ``<INSTITUTION_n>`` — the entity's distinct institution names sorted, numbered from 1;
  * ``<ACCT_MASKED>``   — stands in for a masked account number;
  * ``<ACCOUNT>``       — fallback for an account-built balance-sheet line whose label
    shape is unknown (never pass such a label through).

Pure: reads the accounts table through ``AccountRepo`` only; no network, no file
writes, no CLI imports.
"""
from __future__ import annotations

from typing import Any

from ledger_agent.core.database import AccountRepo

__all__ = [
    "ACCOUNT_FALLBACK",
    "ACCOUNT_LINE_CODES",
    "MASKED_TOKEN",
    "account_label_map",
    "account_line_label",
    "account_tokens",
]

# Balance-sheet line codes whose label is built from institution + account name.
ACCOUNT_LINE_CODES = frozenset({"1010", "1100_acct", "2030"})
ACCOUNT_FALLBACK = "<ACCOUNT>"
MASKED_TOKEN = "<ACCT_MASKED>"


def account_tokens(entity_id: str) -> dict[str, tuple[str, str]]:
    """account_id → (account token, institution token), stable for one DB state."""
    accounts = sorted(AccountRepo.list_for_entity(entity_id), key=lambda a: a.id)
    institutions = sorted({a.institution for a in accounts})
    inst_token = {name: f"<INSTITUTION_{i}>" for i, name in enumerate(institutions, 1)}
    return {
        a.id: (f"<ACCOUNT_{i}>", inst_token[a.institution])
        for i, a in enumerate(accounts, 1)
    }


def account_label_map(entity_id: str) -> dict[str, str]:
    """Raw balance-sheet account labels (see BalanceSheetBuilder) → tokenised labels."""
    tokens = account_tokens(entity_id)
    labels: dict[str, str] = {}
    for a in AccountRepo.list_for_entity(entity_id):
        acct_tok, inst_tok = tokens[a.id]
        labels[f"{a.institution} – {a.name}"] = f"{inst_tok} – {acct_tok}"
        labels[f"Credit Card Payable – {a.institution} ({a.name})"] = (
            f"Credit Card Payable – {inst_tok} ({acct_tok})"
        )
    return labels


def account_line_label(line: Any, label_map: dict[str, str]) -> str | None:
    """Tokenised label for an account-built balance-sheet line, else None.

    A label found in ``label_map`` is replaced by its token label; a line with an
    account line code but an unknown label shape becomes ``ACCOUNT_FALLBACK``.  Any
    other line returns None and the caller decides how to show its label."""
    if line.label in label_map:
        return label_map[line.label]
    if line.coa_code in ACCOUNT_LINE_CODES:
        return ACCOUNT_FALLBACK  # unknown account label shape → never pass it through
    return None

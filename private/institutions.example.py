"""
private/institutions.example.py  –  Template for parser detection tokens
=========================================================================

Copy to ``private/institutions.py`` (gitignored) and fill in the token lists.
This example file is tracked (see the ``!private/institutions.example.py`` rule
in ``.gitignore``) and must only ever contain pseudonymous keys and empty lists.

Each parser in ``ledger_agent/core/parsers/`` imports one dict from here, e.g.::

    from private.institutions import BANK_X as _CFG

If the import fails, or ``detect`` is empty, that parser's ``can_parse()``
returns False, so it will never claim a PDF.

Keys
----
detect
    Strings that must ALL appear (case-insensitive, whitespace ignored) in the
    PDF's extracted text for the parser to claim it.
intra_xfer_keywords
    Description fragments that mark a transaction as an inter-account transfer
    between the entity's own accounts. Read by bank_x_checking, bank_x2_checking,
    bank_x3_checking, bank_x4_checking and broker_y_brokerage. Other ZELLE / WIRE /
    TRANSFER rows are only transfer candidates, confirmed at import by the
    opposite leg in another account of the same entity.

OWNER_DRAW_ACCOUNTS
    Not a parser dict. ``last4`` lists the last 4 digits of the owners' OWN
    accounts at the brokerage; transfers out to these are owner draws (3040),
    not internal transfers. Read by ``ledger_agent/core/intelligence/owner_draws.py``.
    If the name is missing, nothing is reclassified.
"""
# ruff: noqa: N999  (dotted filename is intentional: it is a template, never imported)

BANK_X = {"detect": [], "intra_xfer_keywords": []}   # bank_x_checking (+ broker_y cross-ref)
BANK_X2 = {"detect": [], "intra_xfer_keywords": []}  # bank_x2_checking
BANK_X3 = {"detect": [], "intra_xfer_keywords": []}  # bank_x3_checking
BANK_X4 = {"detect": [], "intra_xfer_keywords": []}  # bank_x4_checking, bank_x4_creditcard
BROKER_Y = {"detect": [], "intra_xfer_keywords": []}  # broker_y_brokerage
BROKER_Z = {"detect": []}                            # broker_z

# Last 4 digits of the owners' OWN accounts at the brokerage; transfers to these are
# owner draws (3040). Real values only in private/institutions.py.
OWNER_DRAW_ACCOUNTS = {"last4": []}

"""
accounting/wash_sale.py  –  Wash-sale disallowance (R-75 / W16)
───────────────────────────────────────────────────────────────
IRC §1091: losses on securities sold at a loss and repurchased within 30 days  # redaction: allow
before or after the sale date are disallowed for that tax year.

The engine cannot read wash-sale adjustment columns from broker PDFs directly
(they live in the 1099-B, which is a private gitignored document).  Instead,
a CSV of adjustments is loaded from ``private/wash_sale_adjustments.csv`` (or
the path given by the ``FI_WASH_SALE_CSV`` environment variable).

In public CI that file is absent; the module gracefully returns a zero
adjustment and logs a warning — the ``test_net_stcg`` parity test remains
xfail in that environment.

CSV format (see ``private/wash_sale_adjustments.example.csv``):
    ticker,disallowed_loss
    TICKER_SEC1,1000.00
    TICKER_SEC2,500.00
"""
from __future__ import annotations

import csv
import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

_DEFAULT_CSV = Path(__file__).resolve().parents[3] / "private" / "wash_sale_adjustments.csv"


def load_adjustments(csv_path: Optional[Path] = None) -> Dict[str, Decimal]:
    """
    Load wash-sale disallowance amounts from CSV.

    Returns a dict mapping ticker symbol → disallowed loss amount (positive =
    amount to ADD BACK to net STCG, making it less negative / more positive).
    Returns an empty dict if the file is absent.
    """
    env_path = os.environ.get("FI_WASH_SALE_CSV", "").strip()
    candidates = []
    if csv_path is not None:
        candidates.append(csv_path)
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(_DEFAULT_CSV)

    for path in candidates:
        if path.exists():
            return _parse_csv(path)

    log.warning(
        "wash_sale: adjustment CSV not found (searched %s). "
        "Net STCG will NOT include wash-sale disallowances. "
        "Set FI_WASH_SALE_CSV or place file at private/wash_sale_adjustments.csv.",
        [str(p) for p in candidates],
    )
    return {}


def _parse_csv(path: Path) -> Dict[str, Decimal]:
    """Parse the wash-sale CSV; skip malformed rows."""
    result: Dict[str, Decimal] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ticker = row.get("ticker", "").strip().upper()
            raw = row.get("disallowed_loss", "").strip()
            if not ticker or not raw:
                continue
            try:
                result[ticker] = Decimal(raw)
            except Exception:
                log.warning("wash_sale: skipped malformed row: %r", row)
    return result


def total_disallowed(csv_path: Optional[Path] = None) -> Decimal:
    """
    Return the total wash-sale disallowance amount across all tickers.

    This is added to (i.e., reduces) the raw net STCG loss bucket to produce
    the CPA-adjusted net short-term capital gain figure.
    """
    adjustments = load_adjustments(csv_path)
    if not adjustments:
        return Decimal("0")
    return sum(adjustments.values(), Decimal("0"))

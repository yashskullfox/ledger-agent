"""
accounting/wash_sale.py  –  Wash-sale disallowance (R-75 / W16)
───────────────────────────────────────────────────────────────
IRC §1091: losses on securities sold at a loss and repurchased within 30 days  # redaction: allow
before or after the sale date are disallowed for that tax year.

Detection strategy (priority order):
  1. DB-based auto-detection from ``realised_trades`` (preferred).
     Looks at each loss trade and checks whether the same symbol was
     traded again within a 30-day window using the ``realised_trades``
     and ``transactions`` tables.  No external file required.
  2. CSV override at ``private/wash_sale_adjustments.csv`` (or
     ``FI_WASH_SALE_CSV`` env var).  Use this when the broker's 1099-B
     contains year-end wash-sale columns that differ from our DB view
     (e.g. cost-basis adjustments the broker calculated after year-end).

CSV format (see ``private/wash_sale_adjustments.example.csv``):
    ticker,disallowed_loss
    TICKER_SEC1,1000.00
    TICKER_SEC2,500.00
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_DEFAULT_CSV = Path(__file__).resolve().parents[3] / "private" / "wash_sale_adjustments.csv"


# ── DB-based detection ────────────────────────────────────────────────────────

def detect_from_db(fiscal_year: int) -> Dict[str, Decimal]:
    """
    Auto-detect wash-sale disallowances from realised_trades in the DB.

    Algorithm (IRC §1091 30-day window):
      For every loss trade (gain_loss < 0) in fiscal_year:
        - Collect all trade dates for the same symbol in the DB
          (across all periods, to catch cross-year window violations)
        - If any other trade date falls within 30 days before OR after
          the settlement_date of the loss trade, the loss is disallowed.
      Returns dict of ticker → total disallowed loss (positive = add back).

    Returns an empty dict when there are no realised_trades for the year.
    """
    try:
        from ledger_agent.core.database import get_conn, init_db
        init_db()
    except Exception as exc:
        log.warning("wash_sale.detect_from_db: DB unavailable (%s); returning empty", exc)
        return {}

    prefix = f"{fiscal_year}-%"
    try:
        with get_conn() as conn:
            # All loss trades in the fiscal year
            loss_rows = conn.execute(
                "SELECT symbol, gain_loss, settlement_date "
                "FROM realised_trades "
                "WHERE statement_period LIKE ? AND CAST(gain_loss AS REAL) < 0 "
                "ORDER BY settlement_date",
                (prefix,),
            ).fetchall()

            if not loss_rows:
                return {}

            # All trade dates for every symbol (wider window ±30 days from year boundary)
            all_symbol_dates: Dict[str, List[str]] = {}
            for row in loss_rows:
                sym = (row["symbol"] or "").upper().strip()
                if not sym or sym in all_symbol_dates:
                    continue
                date_rows = conn.execute(
                    "SELECT DISTINCT settlement_date FROM realised_trades "
                    "WHERE symbol = ? AND settlement_date IS NOT NULL",
                    (sym,),
                ).fetchall()
                all_symbol_dates[sym] = [r["settlement_date"] for r in date_rows if r["settlement_date"]]
    except Exception as exc:
        log.warning("wash_sale.detect_from_db: query failed (%s); returning empty", exc)
        return {}

    disallowed: Dict[str, Decimal] = {}

    for row in loss_rows:
        sym = (row["symbol"] or "").upper().strip()
        if not sym or not row["settlement_date"]:
            continue

        try:
            from datetime import date as date_
            sell_date = date_.fromisoformat(row["settlement_date"])
        except (ValueError, TypeError):
            continue

        loss_amt = abs(Decimal(str(row["gain_loss"])))
        window_start = sell_date - timedelta(days=30)
        window_end = sell_date + timedelta(days=30)

        other_dates = [d for d in all_symbol_dates.get(sym, []) if d != row["settlement_date"]]
        for d_str in other_dates:
            try:
                other_date = date_.fromisoformat(d_str)
            except (ValueError, TypeError):
                continue
            if window_start <= other_date <= window_end:
                # Wash sale: loss disallowed — add back (reduce net loss)
                disallowed[sym] = disallowed.get(sym, Decimal("0")) + loss_amt
                log.info(
                    "wash_sale: disallowing %s loss of %s on %s "
                    "(same-symbol trade within 30 days: %s)",
                    sym, loss_amt, sell_date, other_date,
                )
                break  # one match is enough per loss trade

    if disallowed:
        log.info("wash_sale.detect_from_db: %d ticker(s) with disallowed losses: %s",
                 len(disallowed), list(disallowed.keys()))
    else:
        log.info("wash_sale.detect_from_db: no wash-sale violations found in DB for %d", fiscal_year)

    return disallowed


# ── CSV override ──────────────────────────────────────────────────────────────

def load_adjustments(csv_path: Optional[Path] = None) -> Dict[str, Decimal]:
    """
    Load wash-sale disallowance amounts from CSV override.

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

    log.debug(
        "wash_sale: no CSV override found (searched %s). "
        "Will use DB-based detection.",
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


# ── Public entry point ────────────────────────────────────────────────────────

def total_disallowed(
    csv_path: Optional[Path] = None,
    fiscal_year: Optional[int] = None,
) -> Decimal:
    """
    Return the total wash-sale disallowance amount across all tickers.

    Resolution order:
      1. CSV override (``private/wash_sale_adjustments.csv`` or ``FI_WASH_SALE_CSV``)
         — exact 1099-B year-end figures from broker, takes precedence.
      2. DB-based auto-detection from ``realised_trades`` — computed from
         trade dates using the 30-day window rule (requires fiscal_year).
      3. Zero — no data available.

    The result is ADDED to (i.e., reduces) the raw net STCG loss bucket to
    produce the adjusted net short-term capital gain figure.
    """
    # 1. CSV override takes priority (most accurate: direct from 1099-B)
    csv_adjustments = load_adjustments(csv_path)
    if csv_adjustments:
        total = sum(csv_adjustments.values(), Decimal("0"))
        log.info("wash_sale: using CSV override; total disallowed = %s", total)
        return total

    # 2. DB-based auto-detection
    if fiscal_year is not None:
        db_adjustments = detect_from_db(fiscal_year)
        if db_adjustments:
            total = sum(db_adjustments.values(), Decimal("0"))
            log.info("wash_sale: using DB detection; total disallowed = %s", total)
            return total

    return Decimal("0")

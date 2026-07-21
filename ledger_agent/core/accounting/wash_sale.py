"""
accounting/wash_sale.py  –  Wash-sale disallowance (R-75 / W16)
───────────────────────────────────────────────────────────────
IRC §1091: losses on securities sold at a loss and repurchased within 30 days  # redaction: allow
before or after the sale date are disallowed for that tax year.

This module supports THREE sources of wash-sale disallowance data, in this
preference order:

    1. CSV overlay  (private/wash_sale_adjustments.csv, or FI_WASH_SALE_CSV)
       — hand-curated or 1099-B-derived; authoritative when present.

    2. Computed from own data  (this module, see ``compute_from_ledger``)
       — scans the transactions table for SELL rows with a realised loss
       and cross-references the positions table to detect quantity
       increases in the ±30-day window (approximated to adjacent monthly
       period-ends, since our position snapshots are monthly).

    3. Zero (with a WARNING)  when neither source is available.

The computed path lets us produce a defensible net STCG figure year-round
without waiting for the year-end 1099-B. When the 1099-B eventually lands
and is loaded as CSV, ``reconcile_computed_vs_csv`` returns a per-ticker
delta report so the estimator can learn where its heuristic diverges from
the broker's authoritative disallowance.

Data requirements:
    - transactions table: SELL rows with `tags` containing the ticker
      symbol (parser emits this: ``tags=[term, "realised", symbol]``).
    - positions table:    per-period holdings (symbol, quantity, period).

Limitations of the computed path:
    - Position granularity is monthly; the wash-sale window is 30 days.
      We use adjacent period-end deltas as a conservative approximation.
    - "Substantially identical" security is approximated by exact ticker
      match; options / different share classes are not detected.
    - Cost-basis tracking is per-position aggregate, not per-lot.

All computed and CSV values are in USD, positive = amount to add BACK to
net STCG (making the loss less negative).
"""
from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_DEFAULT_CSV = Path(__file__).resolve().parents[3] / "private" / "wash_sale_adjustments.csv"

# IRS wash-sale window (§1091)
WINDOW_DAYS = 30  # redaction: allow


# --------------------------------------------------------------------------- #
# Data classes                                                                #
# --------------------------------------------------------------------------- #


@dataclass
class LossSaleEvent:
    """A single SELL transaction that produced a realised loss."""
    account_id: str
    trade_date: date
    statement_period: str
    symbol: str
    loss_amount: Decimal   # positive number — magnitude of the loss
    description: str = ""


@dataclass
class WashSaleFinding:
    """A loss sale flagged as (partially) disallowed under §1091."""  # redaction: allow
    symbol: str
    trade_date: date
    loss_amount: Decimal
    disallowed_amount: Decimal    # <= loss_amount
    evidence: str                 # human-readable reason


@dataclass
class WashSaleReport:
    fiscal_year: int
    entity_id: str
    loss_sales_considered: int = 0
    total_loss_at_risk: Decimal = Decimal("0")
    findings: List[WashSaleFinding] = field(default_factory=list)
    total_disallowed: Decimal = Decimal("0")
    source: str = "computed"      # "csv" | "computed" | "combined" | "none"
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fiscal_year": self.fiscal_year,
            "entity_id": self.entity_id,
            "loss_sales_considered": self.loss_sales_considered,
            "total_loss_at_risk": str(self.total_loss_at_risk),
            "total_disallowed": str(self.total_disallowed),
            "source": self.source,
            "findings": [
                {
                    "symbol": f.symbol,
                    "trade_date": f.trade_date.isoformat(),
                    "loss_amount": str(f.loss_amount),
                    "disallowed_amount": str(f.disallowed_amount),
                    "evidence": f.evidence,
                }
                for f in self.findings
            ],
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# CSV overlay (legacy path — kept fully backwards-compatible)                 #
# --------------------------------------------------------------------------- #


def load_adjustments(csv_path: Optional[Path] = None) -> Dict[str, Decimal]:
    """Load wash-sale disallowance amounts from CSV.

    Returns {ticker: disallowed_loss}. Returns {} if the file is absent —
    caller must decide whether to fall back to ``compute_from_ledger``.
    """
    env_path = os.environ.get("FI_WASH_SALE_CSV", "").strip()
    candidates: List[Path] = []
    if csv_path is not None:
        candidates.append(csv_path)
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(_DEFAULT_CSV)

    for path in candidates:
        if path.exists():
            return _parse_csv(path)

    log.debug("wash_sale: adjustment CSV not found (searched %s)",
              [str(p) for p in candidates])
    return {}


def _parse_csv(path: Path) -> Dict[str, Decimal]:
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


# --------------------------------------------------------------------------- #
# Computed path — derive disallowances from own ledger                        #
# --------------------------------------------------------------------------- #


def _next_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    if m == 12:
        return f"{y+1:04d}-01"
    return f"{y:04d}-{m+1:02d}"


def _prev_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    if m == 1:
        return f"{y-1:04d}-12"
    return f"{y:04d}-{m-1:02d}"


def _load_loss_sales(entity_id: str, fiscal_year: int) -> List[LossSaleEvent]:
    """Every SELL transaction with a negative amount (realised loss) for
    ``entity_id`` in ``fiscal_year``. Ticker comes from the parser's
    ``tags`` convention: last uppercase 1-8 char token in ``tags``."""
    from ledger_agent.core.database import get_conn  # deferred: core purity
    events: List[LossSaleEvent] = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.account_id, t.date, t.statement_period, t.description, "
            "       t.amount, t.tags "
            "  FROM transactions t "
            "  JOIN accounts a ON a.id = t.account_id "
            " WHERE a.entity_id = ? "
            "   AND t.transaction_type = 'sell' "
            "   AND t.statement_period LIKE ? "
            "   AND CAST(t.amount AS REAL) < 0 "
            " ORDER BY t.date",
            (entity_id, f"{fiscal_year}-%"),
        ).fetchall()

    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except (TypeError, ValueError):
            tags = []
        symbol = ""
        for candidate in reversed(tags):
            if isinstance(candidate, str) and candidate.isupper() and 1 <= len(candidate) <= 8:
                symbol = candidate
                break
        if not symbol:
            continue
        try:
            d = date.fromisoformat(r["date"])
        except (TypeError, ValueError):
            continue
        events.append(LossSaleEvent(
            account_id=r["account_id"],
            trade_date=d,
            statement_period=r["statement_period"],
            symbol=symbol,
            loss_amount=abs(Decimal(str(r["amount"]))),
            description=r["description"] or "",
        ))
    return events


def _load_position_qty(entity_id: str, period: str, symbol: str) -> Decimal:
    """Total ``quantity`` for ``symbol`` at end of ``period`` across all
    accounts held by ``entity_id``. Returns 0 if no matching row."""
    from ledger_agent.core.database import get_conn  # deferred
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(CAST(p.quantity AS REAL)), 0) AS q "
            "  FROM positions p "
            "  JOIN accounts a ON a.id = p.account_id "
            " WHERE a.entity_id = ? "
            "   AND p.statement_period = ? "
            "   AND UPPER(p.symbol) = ?",
            (entity_id, period, symbol.upper()),
        ).fetchone()
    if not row:
        return Decimal("0")
    return Decimal(str(row["q"] or "0"))


def compute_from_ledger(entity_id: str, fiscal_year: int) -> WashSaleReport:
    """Detect wash-sale suspects from own transaction + position data.

    For each SELL@loss on trade_date D of symbol T:
      * qty_at_sale = position at end of month(D)  (post-sale holding)
      * qty_after   = position at end of month(D)+1  (post-window holding)
      * qty_before  = position at end of month(D)−1  (pre-sale holding)

    Re-buy signal (either fires → mark disallowed):
      (a) qty_after > qty_at_sale  — added shares in the month after
          the loss sale (within the ±30-day window).
      (b) qty_before == 0 and qty_after > 0 — round-trip: opened a new
          position in the same ticker after realising the loss.

    Because we don't track per-lot share counts in the transactions table
    (SELL amount is dollars, not shares), we use a CONSERVATIVE ceiling:
    a triggered finding disallows the full loss for that sale. The 1099-B
    at year-end may report less; the ``reconcile_computed_vs_csv`` helper
    surfaces the delta so the estimator can learn.
    """
    report = WashSaleReport(fiscal_year=fiscal_year, entity_id=entity_id, source="computed")
    events = _load_loss_sales(entity_id, fiscal_year)
    report.loss_sales_considered = len(events)
    report.total_loss_at_risk = sum((e.loss_amount for e in events), Decimal("0"))

    if not events:
        report.notes.append(
            f"no SELL@loss transactions with a recognisable ticker tag were "
            f"found for entity {entity_id} in FY{fiscal_year}"
        )
        return report

    for ev in events:
        prev = _prev_period(ev.statement_period)
        nxt = _next_period(ev.statement_period)
        qty_before = _load_position_qty(entity_id, prev, ev.symbol)
        qty_at_sale = _load_position_qty(entity_id, ev.statement_period, ev.symbol)
        qty_after = _load_position_qty(entity_id, nxt, ev.symbol)

        triggered = False
        evidence = ""
        if qty_after > qty_at_sale:
            triggered = True
            evidence = (
                f"position qty for {ev.symbol} increased "
                f"{qty_at_sale} → {qty_after} between "
                f"{ev.statement_period} and {nxt} (within ±30d window)"
            )
        elif qty_before == 0 and qty_after > 0:
            triggered = True
            evidence = (
                f"round-trip: opened new position in {ev.symbol} in the "
                f"month after realised loss on {ev.trade_date.isoformat()}"
            )

        if triggered:
            report.findings.append(WashSaleFinding(
                symbol=ev.symbol,
                trade_date=ev.trade_date,
                loss_amount=ev.loss_amount,
                disallowed_amount=ev.loss_amount,  # conservative ceiling
                evidence=evidence,
            ))

    report.total_disallowed = sum(
        (f.disallowed_amount for f in report.findings), Decimal("0")
    )

    if not report.findings and report.loss_sales_considered:
        report.notes.append(
            f"{report.loss_sales_considered} loss sale(s) examined; no "
            "wash-sale suspects detected via monthly position deltas — "
            "validate against 1099-B at year-end."
        )
    return report


# --------------------------------------------------------------------------- #
# Reconciliation — computed vs authoritative (1099-B CSV)                     #
# --------------------------------------------------------------------------- #


def reconcile_computed_vs_csv(entity_id: str, fiscal_year: int,
                              csv_path: Optional[Path] = None) -> dict:
    """Return a per-ticker delta dict that contrasts the computed disallowance
    against the CSV (1099-B) overlay. Useful at year-end for calibration."""
    computed = compute_from_ledger(entity_id, fiscal_year)
    csv_data = load_adjustments(csv_path)

    computed_by_ticker: Dict[str, Decimal] = {}
    for f in computed.findings:
        computed_by_ticker[f.symbol] = (
            computed_by_ticker.get(f.symbol, Decimal("0")) + f.disallowed_amount
        )

    all_tickers = set(computed_by_ticker.keys()) | set(csv_data.keys())
    per_ticker = {}
    for tk in sorted(all_tickers):
        c = computed_by_ticker.get(tk, Decimal("0"))
        a = csv_data.get(tk, Decimal("0"))
        per_ticker[tk] = {
            "computed": str(c),
            "authoritative_csv": str(a),
            "delta": str(c - a),
        }

    return {
        "fiscal_year": fiscal_year,
        "entity_id": entity_id,
        "computed_total": str(computed.total_disallowed),
        "csv_total": str(sum(csv_data.values(), Decimal("0"))),
        "per_ticker": per_ticker,
        "csv_present": bool(csv_data),
        "computed_report": computed.as_dict(),
    }


# --------------------------------------------------------------------------- #
# Public helpers (used by api.py)                                             #
# --------------------------------------------------------------------------- #


def total_disallowed(csv_path: Optional[Path] = None,
                     entity_id: Optional[str] = None,
                     fiscal_year: Optional[int] = None) -> Decimal:
    """Total wash-sale disallowance to add back to net STCG.

    Preference order:
        1. CSV overlay if present  (authoritative — from 1099-B).
        2. Computed from own ledger if ``entity_id`` and ``fiscal_year``
           are supplied and we have transaction data.
        3. Zero — with a WARNING.

    Signature is backwards-compatible: callers that only pass ``csv_path``
    (the legacy call) still work; they just skip the computed fallback.
    """
    csv_data = load_adjustments(csv_path)
    if csv_data:
        log.info("wash_sale: using CSV overlay (%d tickers)", len(csv_data))
        return sum(csv_data.values(), Decimal("0"))

    if entity_id and fiscal_year:
        try:
            report = compute_from_ledger(entity_id, fiscal_year)
        except Exception as exc:  # noqa: BLE001 — never let this break parity
            log.warning("wash_sale: computed path failed (%s); using $0", exc)
            return Decimal("0")
        if report.findings:
            log.info(
                "wash_sale: no CSV overlay — using computed disallowance "
                "(%d finding(s), $%s total) from own ledger for FY%d",
                len(report.findings), report.total_disallowed, fiscal_year,
            )
            return report.total_disallowed
        log.info(
            "wash_sale: no CSV, computed path found no suspects across "
            "%d loss sale(s) in FY%d",
            report.loss_sales_considered, fiscal_year,
        )
        return Decimal("0")

    log.warning(
        "wash_sale: no adjustment source (CSV absent, computed path not "
        "invoked). Net STCG will NOT include wash-sale disallowances. "
        "Supply private/wash_sale_adjustments.csv at year-end for the "
        "authoritative 1099-B figures."
    )
    return Decimal("0")


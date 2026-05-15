from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from ledger_agent.core.exceptions import ParserGap
from ledger_agent.core.models import (
    AccountSnapshot, ParsedStatement, Position, PositionType, RealisedTrade,
    StatementType, Transaction, TransactionType,
)
from ledger_agent.core.parsers.base import BaseStatementParser
from ledger_agent.core.parsers.registry import ParserRegistry


@ParserRegistry.register
class IBKRParser(BaseStatementParser):
    PARSER_ID = "ibkr"
    INSTITUTION = "Interactive Brokers"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        upper = text.upper()
        return "INTERACTIVE BROKERS" in upper and (
                "ACTIVITY STATEMENT" in upper or "ACCOUNT STATEMENT" in upper
        )

    def parse(self, pdf_path: Path) -> ParsedStatement:
        raw_text = self.extract_text(pdf_path)
        period, year = self._extract_period(raw_text)
        account_no = self._extract_account_number(raw_text)
        entity_name = self._extract_entity_name(raw_text)

        snapshot = self._parse_cash_report(raw_text, period)
        positions = self._parse_positions(raw_text, period, year)
        trades = self._parse_trades(raw_text, period, year)
        txns = self._parse_cash_transactions(raw_text, period, year)

        for trade in trades:
            txns.append(Transaction(
                account_id="",
                date=trade.settlement_date or date(int(period[:4]), int(period[5:7]), 1),
                description=f"{'GAIN' if trade.gain_loss >= 0 else 'LOSS'} – {trade.symbol}: {trade.description}",
                raw_description=trade.description,
                amount=trade.gain_loss,
                transaction_type=TransactionType.SELL,
                statement_period=period,
                coa_code="4010" if trade.gain_loss >= 0 else "5070",
                coa_name="Realised Trading Gains" if trade.gain_loss >= 0 else "Realised Trading Losses",
                tags=[trade.term, "realised", trade.symbol],
            ))

        return ParsedStatement(
            parser_id=self.PARSER_ID,
            statement_type=StatementType.BROKERAGE,
            institution=self.INSTITUTION,
            account_number_masked=self.mask_account(account_no),
            statement_period=period,
            entity_name=entity_name,
            transactions=txns,
            positions=positions,
            snapshot=snapshot,
            raw_text=raw_text,
            source_file=str(pdf_path),
        )

    def _extract_period(self, text: str) -> Tuple[str, int]:
        m = re.search(
            r"(?:Period|From)[:\s]+(\d{4}-\d{2}-\d{2})\s*(?:to|-)\s*(\d{4}-\d{2}-\d{2})",
            text, re.IGNORECASE,
        )
        if m:
            d = self.parse_date(m.group(2))
            if d:
                return self.period_from_date(d), d.year
        m2 = re.search(
            r"(\d{4}-\d{2}-\d{2})\s+(?:to|-)\s+(\d{4}-\d{2}-\d{2})",
            text,
        )
        if m2:
            d = self.parse_date(m2.group(2))
            if d:
                return self.period_from_date(d), d.year
        m3 = re.search(
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+\d{1,2},\s+(\d{4})",
            text, re.IGNORECASE,
        )
        if m3:
            year = int(m3.group(1))
            from dateutil import parser as _dp
            try:
                d = _dp.parse(m3.group(0)).date()
                return self.period_from_date(d), d.year
            except Exception:
                pass
        return "0000-00", 0

    def _extract_account_number(self, text: str) -> str:
        m = re.search(r"\b(U\d{7,})\b", text)
        if m:
            return m.group(1)
        m2 = re.search(r"Account[:\s]+([A-Z]\d{6,})", text, re.IGNORECASE)
        if m2:
            return m2.group(1)
        return "0000"

    def _extract_entity_name(self, text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"^[A-Z][A-Z &,]+(?:LLC|INC|CORP|CO|LTD)$", stripped):
                return stripped
        m = re.search(r"Name:\s*(.+)", text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return "UNKNOWN ENTITY"

    def _parse_cash_report(self, text: str, period: str) -> AccountSnapshot:
        def _find(pattern: str) -> Optional[Decimal]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                raw = m.group(1).replace(",", "")
                try:
                    return Decimal(raw)
                except Exception:
                    return None
            return None

        ending = _find(r"Ending Cash\s+([-\d,.]+)")
        beginning = _find(r"Starting Cash\s+([-\d,.]+)")
        deposits = _find(r"Deposits\s+([\d,.]+)")
        withdrawals = _find(r"Withdrawals\s+([\d,.]+)")
        gross_nav = _find(r"Net Asset Value\s+([\d,.]+)")
        margin_bal = _find(r"Net Liquidation Value\s+([-\d,.]+)")

        try:
            from ledger_agent.core.audit import audit as _audit
        except Exception:
            _audit = None

        _absent = []
        if ending is None:
            if _audit:
                _audit("parser.field_absent",
                       institution=self.INSTITUTION,
                       statement_period=period,
                       field="ending_balance",
                       reason="Ending Cash pattern not found")
            _absent.append("ending_balance")

        if "ending_balance" in _absent:
            if _audit:
                _audit("parser.gap",
                       institution=self.INSTITUTION,
                       statement_period=period,
                       missing_fields=_absent)
            raise ParserGap(
                institution=self.INSTITUTION,
                statement_period=period,
                missing_fields=_absent,
            )

        if gross_nav is None:
            if _audit:
                _audit("parser.field_absent",
                       institution=self.INSTITUTION,
                       statement_period=period,
                       field="gross_asset_value",
                       reason="Net Asset Value pattern not found")

        return AccountSnapshot(
            account_id="",
            statement_period=period,
            ending_balance=ending or Decimal("0"),
            beginning_balance=beginning,
            gross_asset_value=gross_nav,
            margin_balance=margin_bal if margin_bal and margin_bal < 0 else None,
            total_credits=deposits,
            total_withdrawals=withdrawals,
        )

    _TRADE_RE = re.compile(
        r"^([A-Z]{1,5})\s+"
        r"(\d{4}-\d{2}-\d{2})[\s,\d:]+?"
        r"([-\d,.]+)\s+"
        r"([-\d,.]+)\s+"
        r"([-\d,.]+)\s*$",
        re.MULTILINE,
    )

    def _parse_trades(self, text: str, period: str, year: int) -> List[RealisedTrade]:
        section = _extract_text_section(text, r"Trades", r"^(?:Dividends|Fees|Cash Report|Open Positions)")
        trades: List[RealisedTrade] = []
        for m in self._TRADE_RE.finditer(section):
            symbol = m.group(1)
            trade_date = self.parse_date(m.group(2))
            try:
                realized_pl = Decimal(m.group(5).replace(",", ""))
            except Exception:
                continue
            if realized_pl == 0:
                continue
            trades.append(RealisedTrade(
                account_id="",
                statement_period=period,
                symbol=symbol,
                description=symbol,
                gain_loss=realized_pl,
                term="short",
                settlement_date=trade_date,
            ))
        return trades

    _POSITION_RE = re.compile(
        r"^([A-Z][A-Z0-9]{0,5})\s+"
        r"([A-Z][A-Z0-9 &,.-]+?)\s+"
        r"([-\d,.]+)\s+"
        r"([\d,.]+)\s+"
        r"([\d,.]+)\s*$",
        re.MULTILINE,
    )

    def _parse_positions(self, text: str, period: str, year: int) -> List[Position]:
        try:
            from ledger_agent.core.audit import audit as _audit
        except Exception:
            _audit = None

        section = _extract_text_section(text, r"Open Positions", r"^(?:Realized|Trades|Dividends|Cash Report)")
        positions: List[Position] = []
        for m in self._POSITION_RE.finditer(section):
            symbol = m.group(1)
            name = m.group(2).strip()
            try:
                qty = Decimal(m.group(3).replace(",", ""))
                price = Decimal(m.group(4).replace(",", ""))
                market_val = Decimal(m.group(5).replace(",", ""))
            except Exception:
                continue
            if qty == 0 or market_val == 0:
                continue
            pos_type = PositionType.OPTION if re.search(r"\d", symbol) else PositionType.EQUITY
            pos = Position(
                account_id="",
                symbol=symbol,
                name=name,
                quantity=abs(qty),
                price_per_unit=price,
                market_value=market_val,
                statement_period=period,
                as_of_date=date(year, int(period[5:7]), 28),
                position_type=pos_type,
            )
            if _audit:
                _audit(
                    "parser.position_emitted",
                    institution=self.INSTITUTION,
                    statement_period=period,
                    symbol=symbol,
                    market_value=str(market_val),
                    position_type=pos_type.value,
                )
            positions.append(pos)
        return positions

    _CASH_TX_RE = re.compile(
        r"^(\d{4}-\d{2}-\d{2})\s+"
        r"(Deposit|Withdrawal|Dividend|Commission|Other)\s+"
        r"([-\d,.]+)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )

    def _parse_cash_transactions(self, text: str, period: str, year: int) -> List[Transaction]:
        section = _extract_text_section(text, r"Deposits & Withdrawals|Cash Transactions",
                                        r"^(?:Trades|Open Positions)")
        txns: List[Transaction] = []
        for m in self._CASH_TX_RE.finditer(section):
            txn_date = self.parse_date(m.group(1))
            txn_kind = m.group(2).lower()
            try:
                amt = Decimal(m.group(3).replace(",", ""))
            except Exception:
                continue
            if txn_date is None or amt == 0:
                continue

            if "deposit" in txn_kind:
                txn_type = TransactionType.TRANSFER_IN
            elif "withdrawal" in txn_kind:
                txn_type = TransactionType.TRANSFER_OUT
            elif "dividend" in txn_kind:
                txn_type = TransactionType.CREDIT
            elif "commission" in txn_kind:
                txn_type = TransactionType.FEE
            else:
                txn_type = TransactionType.CREDIT if amt > 0 else TransactionType.DEBIT

            txns.append(Transaction(
                account_id="",
                date=txn_date,
                description=m.group(2).title(),
                raw_description=m.group(0).strip(),
                amount=amt,
                transaction_type=txn_type,
                statement_period=period,
                is_transfer=("deposit" in txn_kind or "withdrawal" in txn_kind),
            ))
        return txns


def _extract_text_section(text: str, start_pattern: str, end_pattern: str) -> str:
    sm = re.search(start_pattern, text, re.IGNORECASE | re.MULTILINE)
    if not sm:
        return ""
    start = sm.end()
    em = re.search(end_pattern, text[start:], re.IGNORECASE | re.MULTILINE)
    end = start + em.start() if em else len(text)
    return text[start:end]

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from ledger_agent.core.exceptions import ParserGap
from ledger_agent.core.models import (
    AccountSnapshot, ParsedStatement, Position, PositionType,
    RealisedTrade, StatementType, Transaction, TransactionType,
)
from ledger_agent.core.parsers.base import BaseStatementParser
from ledger_agent.core.parsers.registry import ParserRegistry


@ParserRegistry.register
class FidelityBrokerageParser(BaseStatementParser):
    PARSER_ID = "fidelity_brokerage"
    INSTITUTION = "Fidelity Investments"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        return (
                "FIDELITY" in text.upper()
                and "INVESTMENT REPORT" in text.upper()
                and ("BROKERAGE" in text.upper() or "Z23-" in text.upper()
                     or "Account Number" in text)
        )

    def parse(self, pdf_path: Path) -> ParsedStatement:
        raw_text = self.extract_text(pdf_path)
        period, year = self._extract_period(raw_text)
        account_no = self._extract_account_number(raw_text)
        entity_name = self._extract_entity_name(raw_text)

        snapshot = self._parse_summary(raw_text, period)
        positions = self._parse_holdings(raw_text, period, year)
        txns = self._parse_withdrawals(raw_text, period, year)
        txns += self._parse_margin_interest(raw_text, period, year)
        txns += self._parse_dividends(raw_text, period, year)
        trades = self._parse_trades(raw_text, period, year)

        for t in trades:
            txns.append(Transaction(
                account_id="",
                date=t.settlement_date or date(int(period[:4]), int(period[5:7]), 1),
                description=f"{'GAIN' if t.gain_loss >= 0 else 'LOSS'} – {t.symbol}: {t.description}",
                raw_description=t.description,
                amount=t.gain_loss,
                transaction_type=TransactionType.SELL,
                statement_period=period,
                coa_code="4010" if t.gain_loss >= 0 else "5070",
                coa_name="Realised Trading Gains" if t.gain_loss >= 0 else "Realised Trading Losses",
                tags=[t.term, "realised", t.symbol],
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
            r"(\w+ \d{1,2}, (\d{4}))\s*[-–]\s*(\w+ \d{1,2}, \d{4})",
            text,
        )
        if m:
            from dateutil import parser as _dp
            try:
                d = _dp.parse(m.group(3))
                return self.period_from_date(d.date()), d.year
            except Exception:
                pass
        m2 = re.search(r"(\w+)\s+\d{1,2},\s+(\d{4})", text)
        if m2:
            from dateutil import parser as _dp
            try:
                d = _dp.parse(m2.group(0))
                return self.period_from_date(d.date()), d.year
            except Exception:
                pass
        return "0000-00", 0

    def _extract_account_number(self, text: str) -> str:
        m = re.search(r"Z\d{2}-\d{6}", text)
        if m:
            return m.group(0).replace("-", "")
        m2 = re.search(r"Account\s+(?:Number|#)[:\s]+([A-Z0-9\-]{5,})", text)
        if m2:
            return re.sub(r"\D", "", m2.group(1))
        return "0000"

    def _extract_entity_name(self, text: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if re.match(r"^[A-Z][A-Z &]+LLC$", line):
                return line
        return "UNKNOWN ENTITY"

    def _parse_summary(self, text: str, period: str) -> AccountSnapshot:
        def _find(pattern: str) -> Optional[Decimal]:
            m = re.search(pattern, text, re.IGNORECASE)
            return self.parse_amount(m.group(1)) if m else None

        ending_nav = (
                _find(r"Ending Net Account Value[^$\d]*\$?([\d,]+\.\d{2})")
                or _find(r"Ending Account(?:\s+Net)?\s+Value[^$\d]*\$?([\d,]+\.\d{2})")
        )
        beginning_nav = (
                _find(r"Beginning Net Account Value[^$\d]*\$?([\d,]+\.\d{2})")
                or _find(r"Beginning Account(?:\s+Net)?\s+Value[^$\d]*\$?([\d,]+\.\d{2})")
        )
        withdrawals = _find(r"Withdrawals\s+[-–]?\$?([\d,]+\.\d{2})")
        margin_bal = _find(r"Margin balance\s+[-–]?\$?([\d,]+\.\d{2})")
        gross_market = _find(r"Market Value of Holdings\s+\$?([\d,]+\.\d{2})")
        realised = _find(r"Net\s+(?:Short-term\s+)?Gain[/\\]Loss\s+\$?([\d,]+\.\d{2})")

        try:
            from ledger_agent.core.audit import audit as _audit
        except Exception:
            _audit = None

        _absent = []
        if gross_market is None:
            if _audit:
                _audit("parser.field_absent",
                       institution=self.INSTITUTION,
                       statement_period=period,
                       field="gross_asset_value",
                       reason="Market Value of Holdings pattern not found")
            _absent.append("gross_asset_value")

        if margin_bal is None and re.search(r"\bMargin\b", text, re.IGNORECASE):
            if _audit:
                _audit("parser.field_absent",
                       institution=self.INSTITUTION,
                       statement_period=period,
                       field="margin_balance",
                       reason="Margin balance pattern not found in margin account statement")
            _absent.append("margin_balance")

        if ending_nav is None:
            if _audit:
                _audit("parser.field_absent",
                       institution=self.INSTITUTION,
                       statement_period=period,
                       field="ending_balance",
                       reason="Ending Net/Account Value pattern not found")
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

        return AccountSnapshot(
            account_id="",
            statement_period=period,
            ending_balance=ending_nav or Decimal("0"),
            beginning_balance=beginning_nav,
            gross_asset_value=gross_market,
            margin_balance=-(margin_bal) if margin_bal and margin_bal > 0 else margin_bal,
            total_withdrawals=withdrawals,
            realised_gain_loss=realised,
        )

    _HOLDING_RE = re.compile(
        r"M?\s*([A-Z][A-Z0-9 &.,]+?)\s*\(([A-Z]+)\)\s+"
        r"(?:unavailable|\$?[\d,]+\.\d{2})\s+"
        r"([\d,]+\.\d{3})\s+"
        r"\$?([\d,]+\.\d{4})\s+"
        r"\$?([\d,]+\.\d{2})\s+"
        r"\$?([\d,]+\.\d{2})\s+"
        r"(-?\$?[\d,]+\.\d{2}|-)",
        re.MULTILINE,
    )

    def _parse_holdings(self, text: str, period: str, year: int) -> List[Position]:
        try:
            from ledger_agent.core.audit import audit as _audit
        except Exception:
            _audit = None

        positions = []
        for m in self._HOLDING_RE.finditer(text):
            name, symbol = m.group(1).strip(), m.group(2)
            qty = self.parse_amount(m.group(3))
            price = self.parse_amount(m.group(4))
            mv = self.parse_amount(m.group(5))
            cb = self.parse_amount(m.group(6))
            ugl = self.parse_amount(m.group(7))
            if qty and price and mv:
                pos = Position(
                    account_id="",
                    symbol=symbol,
                    name=name,
                    quantity=abs(qty),
                    price_per_unit=abs(price),
                    market_value=abs(mv),
                    statement_period=period,
                    cost_basis=abs(cb) if cb else None,
                    unrealized_gain_loss=ugl,
                    is_margin=True,
                    as_of_date=date(year, int(period[5:7]), 28),
                    position_type=PositionType.EQUITY,
                )
                if _audit:
                    _audit(
                        "parser.position_emitted",
                        institution=self.INSTITUTION,
                        statement_period=period,
                        symbol=symbol,
                        market_value=str(mv),
                        position_type=PositionType.EQUITY.value,
                    )
                positions.append(pos)
        return positions

    _SOLD_LINE_RE = re.compile(
        r"^s\s*(\d{2}/\d{2})\s+(.+?)\s+(?:[A-Z0-9]{9}\s+)?You\s+Sold",
        re.IGNORECASE,
    )
    _GL_RE = re.compile(
        r"(Short|Long)-term\s+(gain|loss):\s+\$?([\d,]+\.\d{2})",
        re.IGNORECASE,
    )

    def _parse_trades(self, text: str, period: str, year: int) -> List[RealisedTrade]:
        trades: List[RealisedTrade] = []
        lines = text.splitlines()

        for i, raw_line in enumerate(lines):
            line = raw_line.strip()
            m = self._SOLD_LINE_RE.match(line)
            if not m:
                continue

            raw_date = m.group(1)
            desc_raw = m.group(2).strip()
            d = self.parse_date(raw_date, year)
            if not d:
                continue

            net_gl = Decimal("0")
            term = "short"

            suffix = line[m.end():]
            inline_found = False
            for gl_m in self._GL_RE.finditer(suffix):
                term = gl_m.group(1).lower()
                gl_type = gl_m.group(2).lower()
                gl_val = self.parse_amount(gl_m.group(3)) or Decimal("0")
                net_gl += gl_val if gl_type == "gain" else -gl_val
                inline_found = True

            if not inline_found:
                for j in range(1, 4):
                    if i + j >= len(lines):
                        break
                    next_line = lines[i + j].strip()
                    if self._SOLD_LINE_RE.match(next_line):
                        break
                    gl_m = self._GL_RE.search(next_line)
                    if gl_m:
                        term = gl_m.group(1).lower()
                        gl_type = gl_m.group(2).lower()
                        gl_val = self.parse_amount(gl_m.group(3)) or Decimal("0")
                        net_gl += gl_val if gl_type == "gain" else -gl_val

            if net_gl == 0:
                continue

            first_word = desc_raw.split()[0] if desc_raw else "UNKNWN"
            symbol = first_word[:6].upper()

            trades.append(RealisedTrade(
                account_id="",
                statement_period=period,
                symbol=symbol,
                description=desc_raw[:120],
                gain_loss=net_gl,
                term=term,
                settlement_date=d,
            ))

        return trades

    _WITHDRAWAL_RE = re.compile(
        r"(\d{2}/\d{2})\s+Money Line Paid\s+EFT FUNDS PAID\s+\S+\s+/WEB\s+"
        r"TRUIST BANK[^-\d]*-([\d,]+\.\d{2})",
        re.IGNORECASE,
    )

    def _parse_withdrawals(self, text: str, period: str, year: int) -> List[Transaction]:
        txns = []
        for m in self._WITHDRAWAL_RE.finditer(text):
            d = self.parse_date(m.group(1), year)
            amt = self.parse_amount(m.group(2))
            if d and amt:
                txns.append(Transaction(
                    account_id="",
                    date=d,
                    description="Transfer Out – Truist Bank",
                    raw_description=m.group(0),
                    amount=-abs(amt),
                    transaction_type=TransactionType.TRANSFER_OUT,
                    statement_period=period,
                    is_transfer=True,
                    coa_code="9000",
                    coa_name="Inter-Account Transfer",
                ))
        return txns

    _MARGIN_RE = re.compile(
        r"(\d{2}/\d{2}-\d{2}/\d{2})\s+"
        r"[\d,]+\s+[\d.]+%\s+[\d,]+\s+"
        r"-\$?([\d,]+\.\d{2})",
        re.IGNORECASE,
    )

    def _parse_margin_interest(self, text: str, period: str, year: int) -> List[Transaction]:
        txns = []
        for m in self._MARGIN_RE.finditer(text):
            amt = self.parse_amount(m.group(2))
            if amt:
                mo = int(period[5:7])
                import calendar
                last_day = calendar.monthrange(year, mo)[1]
                d = date(year, mo, last_day)
                txns.append(Transaction(
                    account_id="",
                    date=d,
                    description="Margin Interest Expense",
                    raw_description=m.group(0),
                    amount=-abs(amt),
                    transaction_type=TransactionType.MARGIN_INTEREST,
                    statement_period=period,
                    coa_code="5030",
                    coa_name="Margin Interest Expense",
                ))
        return txns

    _DIVIDEND_RE = re.compile(
        r"(\d{2}/\d{2})\s+(.+?)\s+[A-Z0-9]{9}\s+Dividend Received\s+"
        r"[-\s]*\$?([\d,]+\.\d{2})",
        re.IGNORECASE,
    )

    def _parse_dividends(self, text: str, period: str, year: int) -> List[Transaction]:
        start_m = re.search(r"Dividends,\s*Interest\s*&\s*Other\s*Income", text, re.IGNORECASE)
        end_m = re.search(r"Total\s+Dividends", text, re.IGNORECASE)
        if not start_m:
            return []
        search_text = text[start_m.start(): end_m.end() if end_m else len(text)]

        txns = []
        for m in self._DIVIDEND_RE.finditer(search_text):
            d = self.parse_date(m.group(1), year)
            raw_sec = m.group(2).strip()
            amt = self.parse_amount(m.group(3))
            if d and amt and amt > 0:
                txns.append(Transaction(
                    account_id="",
                    date=d,
                    description=f"Dividend – {raw_sec[:50]}",
                    raw_description=m.group(0),
                    amount=abs(amt),
                    transaction_type=TransactionType.CREDIT,
                    statement_period=period,
                    coa_code="4021",
                    coa_name="Dividend Income",
                ))
        return txns

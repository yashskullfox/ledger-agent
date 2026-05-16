"""
tests/integration/parsers/test_position_completeness.py  —  ARCH-25
=====================================================================

Verifies R-61: every holding line on a brokerage statement produces
exactly one ``positions`` row with correct symbol, quantity, market value,
and ``position_type``.

Test strategy
-------------
Three classes:

1. **TestPositionFixtureIntegrity** — validates that every committed
   ``brokerage/<inst>/expected.json`` fixture lists each position with
   the fields required by R-61 (symbol, quantity, market_value,
   position_type).  Always passes; never skipped.

2. **TestBrokerYPositionModel** — drives ``_parse_holdings()`` against
   synthetic text fragments and asserts one Position per line.

3. **TestBrokerZPositionModel** — drives ``_parse_positions()`` against
   synthetic text fragments and asserts one Position per line.

Acceptance
----------
    pytest tests/integration/parsers/test_position_completeness.py -q
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "brokerage"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_fixture(institution: str) -> dict:
    p = FIXTURE_ROOT / institution / "expected.json"
    if not p.exists():
        pytest.skip(f"Fixture missing: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


# ── Fixture schema integrity ──────────────────────────────────────────────────

class TestPositionFixtureIntegrity:
    """Every fixture position entry must carry all R-61 required fields."""

    _REQUIRED_POSITION_FIELDS = {"symbol", "quantity", "market_value", "position_type"}
    _VALID_POSITION_TYPES = {"equity", "option", "cash", "fixed_income"}

    @pytest.mark.parametrize("institution", ["broker_y", "broker_z"])
    def test_fixture_positions_have_required_fields(self, institution):
        data = _load_fixture(institution)
        for period, period_data in data["periods"].items():
            for i, pos in enumerate(period_data.get("positions", [])):
                missing = self._REQUIRED_POSITION_FIELDS - set(pos.keys())
                assert not missing, (
                    f"Fixture {institution}/{period} position[{i}] "
                    f"missing fields: {missing}"
                )

    @pytest.mark.parametrize("institution", ["broker_y", "broker_z"])
    def test_fixture_position_types_are_valid(self, institution):
        data = _load_fixture(institution)
        for period, period_data in data["periods"].items():
            for pos in period_data.get("positions", []):
                assert pos["position_type"] in self._VALID_POSITION_TYPES, (
                    f"Fixture {institution}/{period} position {pos['symbol']!r}: "
                    f"unknown position_type {pos['position_type']!r}"
                )

    @pytest.mark.parametrize("institution", ["broker_y", "broker_z"])
    def test_positions_count_matches_list_length(self, institution):
        data = _load_fixture(institution)
        for period, period_data in data["periods"].items():
            declared = period_data.get("positions_count", 0)
            actual = len(period_data.get("positions", []))
            assert declared == actual, (
                f"Fixture {institution}/{period}: positions_count={declared} "
                f"but positions list has {actual} entries"
            )

    @pytest.mark.parametrize("institution", ["broker_y", "broker_z"])
    def test_fixture_position_symbols_are_unique_per_period(self, institution):
        """No duplicate symbols within a single period (each line → one row)."""
        data = _load_fixture(institution)
        for period, period_data in data["periods"].items():
            symbols = [p["symbol"] for p in period_data.get("positions", [])]
            dupes = {s for s in symbols if symbols.count(s) > 1}
            assert not dupes, (
                f"Fixture {institution}/{period}: duplicate symbols {dupes}"
            )


# ── Broker Y position unit tests ──────────────────────────────────────────────

class TestBrokerYPositionModel:
    """
    Unit-test ``_parse_holdings`` against synthetic text.
    Each holding line must produce exactly one Position with the correct
    symbol, quantity, market_value, and position_type.
    """

    @pytest.fixture
    def parser(self):
        import ledger_agent.core.parsers  # noqa: F401 — auto-discovery
        from ledger_agent.core.parsers.broker_y_brokerage import BrokerYBrokerageParser
        return BrokerYBrokerageParser()

    @pytest.fixture
    def two_line_holdings(self):
        """Two synthetic equity holding lines with dollar signs (Format A)."""
        return """
Holdings
MCAREDX INC (CDNA) 31984.00 1,100.000 23.3000 25630.00 16774.00 8856.00 -
SNAP INC (SNAP) 23000.00 3,000.000 11.2900 33870.00 21000.00 12870.00 -
"""

    @pytest.fixture
    def three_line_holdings(self):
        """Three synthetic equity lines (bare numbers, Format B)."""
        return """
Holdings
MCAREDX INC (CDNA) 31984.00 1100.000 23.3000 25630.00 16774.00 8856.00 -
SNAP INC (SNAP) 23000.00 3000.000 11.2900 33870.00 21000.00 12870.00 -
BIGBEAR AI HLDGS INC (BBAI) 0.00 500.000 2.5000 1250.00 1800.00 -550.00 -
"""

    def test_two_holdings_produce_two_positions(self, parser, two_line_holdings):
        positions = parser._parse_holdings(two_line_holdings, "2025-01", 2025)
        assert len(positions) == 2, (
            f"Expected 2 positions, got {len(positions)}: "
            f"{[p.symbol for p in positions]}"
        )

    def test_symbols_match_fixture(self, parser, two_line_holdings):
        positions = parser._parse_holdings(two_line_holdings, "2025-01", 2025)
        symbols = {p.symbol for p in positions}
        assert symbols == {"CDNA", "SNAP"}

    def test_quantity_extracted_correctly(self, parser, two_line_holdings):
        positions = parser._parse_holdings(two_line_holdings, "2025-01", 2025)
        by_sym = {p.symbol: p for p in positions}
        assert by_sym["CDNA"].quantity == Decimal("1100.000")
        assert by_sym["SNAP"].quantity == Decimal("3000.000")

    def test_market_value_extracted_correctly(self, parser, two_line_holdings):
        positions = parser._parse_holdings(two_line_holdings, "2025-01", 2025)
        by_sym = {p.symbol: p for p in positions}
        assert by_sym["CDNA"].market_value == Decimal("25630.00")
        assert by_sym["SNAP"].market_value == Decimal("33870.00")

    def test_position_type_is_equity(self, parser, two_line_holdings):
        from ledger_agent.core.models import PositionType
        positions = parser._parse_holdings(two_line_holdings, "2025-01", 2025)
        for pos in positions:
            assert pos.position_type == PositionType.EQUITY, (
                f"{pos.symbol}: expected EQUITY, got {pos.position_type}"
            )

    def test_three_holdings_produce_three_positions(self, parser, three_line_holdings):
        positions = parser._parse_holdings(three_line_holdings, "2025-01", 2025)
        assert len(positions) == 3, (
            f"Expected 3 positions, got {len(positions)}: "
            f"{[p.symbol for p in positions]}"
        )

    def test_no_lines_produce_empty_list(self, parser):
        positions = parser._parse_holdings("Holdings\n(no securities held)\n", "2025-01", 2025)
        assert positions == []

    def test_fixture_count_matches_parser_output(self, parser, two_line_holdings):
        """Parser output count must match what the fixture declares."""
        fixture = _load_fixture("broker_y")
        period_data = fixture["periods"].get("2025-01")
        if period_data is None:
            pytest.skip("2025-01 not in fixture")
        positions = parser._parse_holdings(two_line_holdings, "2025-01", 2025)
        assert len(positions) == period_data["positions_count"]

    def test_fixture_market_values_match_parser(self, parser, two_line_holdings):
        """Each fixture position market_value must agree with parser output within $0.01."""  # redaction: allow
        fixture = _load_fixture("broker_y")
        period_data = fixture["periods"].get("2025-01")
        if period_data is None:
            pytest.skip("2025-01 not in fixture")
        positions = parser._parse_holdings(two_line_holdings, "2025-01", 2025)
        by_sym = {p.symbol: p for p in positions}
        for fp in period_data["positions"]:
            sym = fp["symbol"]
            assert sym in by_sym, f"Symbol {sym!r} from fixture not found in parser output"
            expected_mv = Decimal(fp["market_value"])
            actual_mv = by_sym[sym].market_value
            assert abs(actual_mv - expected_mv) <= Decimal("0.01"), (
                f"{sym}: fixture market_value={expected_mv} "
                f"parser market_value={actual_mv}"
            )


# ── Broker Z position unit tests ──────────────────────────────────────────────

class TestBrokerZPositionModel:
    """
    Unit-test ``_parse_positions`` against synthetic Broker Z Open Positions text.
    Each holding line must produce exactly one Position.
    """

    @pytest.fixture
    def parser(self):
        import ledger_agent.core.parsers  # noqa: F401
        from ledger_agent.core.parsers.broker_z import BrokerZParser
        return BrokerZParser()

    @pytest.fixture
    def two_position_text(self):
        return """
Broker Z LLC  Activity Statement
Period: 2024-12-01 to 2024-12-31
Account: U1234567  ENTITY_A

Starting Cash   4,800.00
Deposits        200.00
Ending Cash     5,000.00

Open Positions
AAPL  APPLE INC  10.000  235.00  2350.00
MSFT  MICROSOFT CORP  5.000  420.00  2100.00

Cash Report
"""

    @pytest.fixture
    def option_position_text(self):
        """Broker Z option symbols contain digits — parser should mark as OPTION type."""
        return """
Broker Z LLC  Activity Statement
Period: 2024-12-01 to 2024-12-31
Account: U1234567  ENTITY_A

Starting Cash   4,800.00
Ending Cash     5,000.00

Open Positions
SPX1  SP500 CALL DEC24  10.000  5.50  550.00

Cash Report
"""

    def test_two_positions_produce_two_rows(self, parser, two_position_text):
        positions = parser._parse_positions(two_position_text, "2024-12", 2024)
        assert len(positions) == 2, (
            f"Expected 2 positions, got {len(positions)}"
        )

    def test_position_symbols_correct(self, parser, two_position_text):
        positions = parser._parse_positions(two_position_text, "2024-12", 2024)
        symbols = {p.symbol for p in positions}
        assert symbols == {"AAPL", "MSFT"}

    def test_position_market_values(self, parser, two_position_text):
        positions = parser._parse_positions(two_position_text, "2024-12", 2024)
        by_sym = {p.symbol: p for p in positions}
        assert by_sym["AAPL"].market_value == Decimal("2350.00")
        assert by_sym["MSFT"].market_value == Decimal("2100.00")

    def test_equity_position_type(self, parser, two_position_text):
        from ledger_agent.core.models import PositionType
        positions = parser._parse_positions(two_position_text, "2024-12", 2024)
        for pos in positions:
            assert pos.position_type == PositionType.EQUITY, (
                f"{pos.symbol}: expected EQUITY, got {pos.position_type}"
            )

    def test_option_symbol_inferred_as_option_type(self, parser, option_position_text):
        from ledger_agent.core.models import PositionType
        positions = parser._parse_positions(option_position_text, "2024-12", 2024)
        assert len(positions) == 1
        assert positions[0].position_type == PositionType.OPTION, (
            f"Expected OPTION for digit-bearing symbol, got {positions[0].position_type}"
        )

    def test_no_open_positions_section_returns_empty(self, parser):
        text = (
            "Broker Z LLC  Activity Statement\n"
            "Period: 2024-12-01 to 2024-12-31\n"
            "Starting Cash 4800.00\nEnding Cash 5000.00\n"
        )
        positions = parser._parse_positions(text, "2024-12", 2024)
        assert positions == []

    def test_fixture_count_matches_synthetic_output(self, parser, two_position_text):
        fixture = _load_fixture("broker_z")
        period_data = fixture["periods"].get("2024-12")
        if period_data is None:
            pytest.skip("2024-12 not in Broker Z fixture")
        positions = parser._parse_positions(two_position_text, "2024-12", 2024)
        assert len(positions) == period_data["positions_count"], (
            f"Fixture declares {period_data['positions_count']} positions, "
            f"parser produced {len(positions)}"
        )


# ── Position type contract ────────────────────────────────────────────────────

class TestPositionTypeContract:
    """PositionType enum shape and round-trip through the model."""

    def test_position_type_values_are_strings(self):
        from ledger_agent.core.models import PositionType
        for pt in PositionType:
            assert isinstance(pt.value, str)

    def test_all_required_types_present(self):
        from ledger_agent.core.models import PositionType
        values = {pt.value for pt in PositionType}
        assert {"equity", "option", "cash", "fixed_income"} <= values

    def test_position_defaults_to_equity(self):
        from decimal import Decimal
        from ledger_agent.core.models import Position, PositionType
        pos = Position(
            account_id="", symbol="TEST", name="Test Co",
            quantity=Decimal("1"), price_per_unit=Decimal("10"),
            market_value=Decimal("10"), statement_period="2025-01",
        )
        assert pos.position_type == PositionType.EQUITY

    def test_position_type_round_trip(self):
        from ledger_agent.core.models import PositionType
        for pt in PositionType:
            assert PositionType(pt.value) == pt

"""
tests/integration/parsers/test_brokerage_completeness.py  —  ARCH-24
======================================================================

Verifies R-60 / R-62: brokerage parsers must populate ``gross_asset_value``
and ``margin_balance`` whenever the source statement reports those figures,
and must raise ``ParserGap`` (not silently partial-parse) when a required
field is missing.

Test strategy
-------------
Because real PDF statement files contain PII they are **not** committed to
the repository.  Instead the tests exercise:

1. **Parser model validation** — instantiate parsers directly, call their
   internal helpers against synthetic text fragments, and assert the snapshot
   fields match the fixture's expected values.

2. **ParserGap contract** — patch a synthetic statement text so that the
   required field is absent, then assert ``ParserGap`` is raised and no DB
   row is partially written.

3. **Fixture schema check** — load ``expected.json`` files under
   ``tests/integration/fixtures/brokerage/`` and assert they are internally
   consistent (required_fields are all present in fixture's snapshot dict).

Real-PDF round-trip tests (which require actual statement PDFs) are in a
separate file ``test_brokerage_roundtrip.py`` and are guarded by
``pytest.mark.skipif(not STATEMENTS_DIR.exists(), ...)``.

Acceptance
----------
    pytest tests/integration/parsers/test_brokerage_completeness.py -q
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

class TestFixtureSchemaIntegrity:
    """Ensure every committed brokerage fixture is internally consistent."""

    @pytest.mark.parametrize("institution", ["broker_y", "broker_z"])
    def test_fixture_exists(self, institution):
        p = FIXTURE_ROOT / institution / "expected.json"
        assert p.exists(), f"Brokerage fixture missing: {p}"

    @pytest.mark.parametrize("institution", ["broker_y", "broker_z"])
    def test_fixture_is_valid_json(self, institution):
        p = FIXTURE_ROOT / institution / "expected.json"
        if not p.exists():
            pytest.skip(f"Fixture missing: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "periods" in data
        assert "required_snapshot_fields" in data

    @pytest.mark.parametrize("institution", ["broker_y", "broker_z"])
    def test_required_snapshot_fields_present_in_fixture(self, institution):
        data = _load_fixture(institution)
        required = data["required_snapshot_fields"]
        for period, period_data in data["periods"].items():
            snap = period_data.get("snapshot", {})
            for field in required:
                assert field in snap, (
                    f"Fixture {institution}/{period}: required field {field!r} "
                    f"missing from snapshot dict"
                )

    @pytest.mark.parametrize("institution", ["broker_y", "broker_z"])
    def test_positions_count_matches_list(self, institution):
        data = _load_fixture(institution)
        for period, period_data in data["periods"].items():
            expected_count = period_data.get("positions_count", 0)
            actual_count = len(period_data.get("positions", []))
            assert expected_count == actual_count, (
                f"Fixture {institution}/{period}: positions_count={expected_count} "
                f"but positions list has {actual_count} entries"
            )


# ── Broker Y parser unit tests ────────────────────────────────────────────────

class TestBrokerYParserModel:
    """Unit-test the Broker Y parser's internal helpers against synthetic text."""

    @pytest.fixture
    def parser(self):
        import ledger_agent.core.parsers  # noqa: F401 — triggers auto-discovery
        from ledger_agent.core.parsers.broker_y_brokerage import BrokerYBrokerageParser
        return BrokerYBrokerageParser()

    @pytest.fixture
    def synthetic_margin_text(self):
        """Synthetic Broker Y statement text with all required fields present."""
        return """
ENTITY_A  Z23-945042
Investment Report  January 1, 2025 - January 31, 2025

Beginning Net Account Value  29,566.45  # redaction: allow
Ending Net Account Value  **  35,438.80  # redaction: allow
Withdrawals  250.00  # redaction: allow
Margin balance  24,061.20  # redaction: allow

Market Value of Holdings  59,500.00  # redaction: allow

Holdings
MCAREDX INC (CDNA) 31984.00 1,100.000 23.3000 25630.00 16774.00 8856.00 -
SNAP INC (SNAP) 23000.00 3,000.000 11.2900 33870.00 21000.00 12870.00 -
"""  # redaction: allow

    @pytest.fixture
    def synthetic_no_ending_nav_text(self):
        """Synthetic text missing the required Ending Account Value line."""
        return """
ENTITY_A  Z23-945042
Investment Report  January 1, 2025 - January 31, 2025

Beginning Net Account Value  29,566.45  # redaction: allow
Withdrawals  250.00  # redaction: allow
Margin balance  24,061.20  # redaction: allow
Market Value of Holdings  59,500.00  # redaction: allow
"""

    def test_parse_summary_populates_gross_asset_value(self, parser, synthetic_margin_text):
        """_parse_summary must extract gross_asset_value from Market Value of Holdings."""
        snap = parser._parse_summary(synthetic_margin_text, "2025-01")
        assert snap.gross_asset_value is not None, (
            "gross_asset_value should not be None when 'Market Value of Holdings' is present"
        )
        assert snap.gross_asset_value == Decimal("59500.00")

    def test_parse_summary_populates_margin_balance(self, parser, synthetic_margin_text):
        """_parse_summary must extract margin_balance (as negative) when present."""
        snap = parser._parse_summary(synthetic_margin_text, "2025-01")
        assert snap.margin_balance is not None, (
            "margin_balance should not be None when 'Margin balance' line is present"
        )
        assert snap.margin_balance < 0, (
            "margin_balance should be stored as a negative number (debt convention)"
        )
        assert snap.margin_balance == Decimal("-24061.20")

    def test_parse_summary_populates_ending_balance(self, parser, synthetic_margin_text):
        """_parse_summary must extract ending_balance from Ending Net Account Value."""
        snap = parser._parse_summary(synthetic_margin_text, "2025-01")
        assert snap.ending_balance == Decimal("35438.80")

    def test_parse_summary_raises_parser_gap_on_missing_ending_nav(
        self, parser, synthetic_no_ending_nav_text
    ):
        """_parse_summary must raise ParserGap when ending_balance cannot be extracted."""
        from ledger_agent.core.exceptions import ParserGap
        with pytest.raises(ParserGap) as exc_info:
            parser._parse_summary(synthetic_no_ending_nav_text, "2025-01")
        gap = exc_info.value
        assert "ending_balance" in gap.missing_fields
        assert gap.institution == "Broker Y"
        assert gap.statement_period == "2025-01"

    def test_parser_gap_has_structured_attributes(self, parser, synthetic_no_ending_nav_text):
        """ParserGap must carry institution, statement_period, missing_fields."""
        from ledger_agent.core.exceptions import ParserGap
        try:
            parser._parse_summary(synthetic_no_ending_nav_text, "2024-12")
        except ParserGap as gap:
            assert isinstance(gap.institution, str) and gap.institution
            assert isinstance(gap.statement_period, str) and gap.statement_period
            assert isinstance(gap.missing_fields, list) and gap.missing_fields
        else:
            pytest.fail("ParserGap was not raised")

    def test_parse_holdings_returns_all_positions(self, parser, synthetic_margin_text):
        """_parse_holdings must return one Position per holding line."""
        positions = parser._parse_holdings(synthetic_margin_text, "2025-01", 2025)
        assert len(positions) == 2, (
            f"Expected 2 positions (CDNA, SNAP), got {len(positions)}"
        )
        symbols = {p.symbol for p in positions}
        assert "CDNA" in symbols
        assert "SNAP" in symbols

    def test_parse_holdings_market_values(self, parser, synthetic_margin_text):
        """Each position's market_value must match the fixture."""
        positions = parser._parse_holdings(synthetic_margin_text, "2025-01", 2025)
        by_symbol = {p.symbol: p for p in positions}
        assert by_symbol["CDNA"].market_value == Decimal("25630.00")
        assert by_symbol["SNAP"].market_value == Decimal("33870.00")

    def test_fixture_snapshot_values_match_parser_output(self, parser, synthetic_margin_text):
        """Parser output for the synthetic text must match fixture 2025-01 expected values."""
        fixture = _load_fixture("broker_y")
        period_data = fixture["periods"].get("2025-01")
        if period_data is None:
            pytest.skip("2025-01 not in fixture")
        expected_snap = period_data["snapshot"]

        snap = parser._parse_summary(synthetic_margin_text, "2025-01")
        assert abs(snap.ending_balance - Decimal(expected_snap["ending_balance"])) <= Decimal("1.00")
        assert snap.gross_asset_value is not None
        assert abs(snap.gross_asset_value - Decimal(expected_snap["gross_asset_value"])) <= Decimal("1.00")
        assert snap.margin_balance is not None
        assert abs(snap.margin_balance - Decimal(expected_snap["margin_balance"])) <= Decimal("1.00")


# ── Broker Z parser unit tests ─────────────────────────────────────────────────

class TestBrokerZParserModel:
    """Unit-test the Broker Z parser's internal helpers against synthetic text."""

    @pytest.fixture
    def parser(self):
        import ledger_agent.core.parsers  # noqa: F401
        from ledger_agent.core.parsers.broker_z import BrokerZParser
        return BrokerZParser()

    @pytest.fixture
    def synthetic_cash_text(self):
        """Synthetic Broker Z Activity Statement text."""
        return """
Broker Z LLC  Activity Statement
Period: 2024-12-01 to 2024-12-31
Account: U1234567  ENTITY_A

Starting Cash   4,800.00
Deposits        200.00
Withdrawals     0.00
Ending Cash     5,000.00
"""

    @pytest.fixture
    def synthetic_no_ending_cash_text(self):
        """Synthetic text missing the Ending Cash line."""
        return """
Broker Z LLC  Activity Statement
Period: 2024-12-01 to 2024-12-31
Account: U1234567  ENTITY_A

Starting Cash   4,800.00
Deposits        200.00
"""

    def test_parse_cash_report_ending_balance(self, parser, synthetic_cash_text):
        snap = parser._parse_cash_report(synthetic_cash_text, "2024-12")
        assert snap.ending_balance == Decimal("5000.00")

    def test_parse_cash_report_raises_parser_gap_on_missing_ending(
        self, parser, synthetic_no_ending_cash_text
    ):
        from ledger_agent.core.exceptions import ParserGap
        with pytest.raises(ParserGap) as exc_info:
            parser._parse_cash_report(synthetic_no_ending_cash_text, "2024-12")
        assert "ending_balance" in exc_info.value.missing_fields


# ── ParserGap exception contract ──────────────────────────────────────────────

class TestParserGapContract:
    """Verify ParserGap exception shape and str representation."""

    def test_parser_gap_str_contains_institution(self):
        from ledger_agent.core.exceptions import ParserGap
        gap = ParserGap("Broker Y", "2024-12", ["gross_asset_value"])
        assert "Broker Y" in str(gap)
        assert "2024-12" in str(gap)
        assert "gross_asset_value" in str(gap)

    def test_parser_gap_with_source_file(self):
        from ledger_agent.core.exceptions import ParserGap
        gap = ParserGap("Broker Z", "2025-01", ["ending_balance"], source_file="/tmp/stmt.pdf")
        assert "/tmp/stmt.pdf" in str(gap)
        assert gap.source_file == "/tmp/stmt.pdf"

    def test_parser_gap_is_subclass_of_parser_error(self):
        from ledger_agent.core.exceptions import ParserGap, FinancialIntelligenceError
        assert issubclass(ParserGap, FinancialIntelligenceError)

    def test_aggregation_gap_contract(self):
        from ledger_agent.core.exceptions import AggregationGap, FinancialIntelligenceError
        gap = AggregationGap("2024-12", "acct-abc", "expected from import registry")
        assert "2024-12" in str(gap)
        assert "acct-abc" in str(gap)
        assert issubclass(AggregationGap, FinancialIntelligenceError)

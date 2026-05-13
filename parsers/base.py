"""
parsers/base.py  –  Abstract base class for all statement parsers
─────────────────────────────────────────────────────────────────
To add a new institution:
  1. Subclass BaseStatementParser
  2. Implement `can_parse()` and `parse()`
  3. Decorate with @ParserRegistry.register
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import pdfplumber

from core.models import ParsedStatement

class BaseStatementParser(ABC):
    """All statement parsers must implement this interface."""

    PARSER_ID: str = ""          # unique slug, e.g. "truist_checking"
    INSTITUTION: str = ""        # human-readable, e.g. "Truist Bank"

    @classmethod
    @abstractmethod
    def can_parse(cls, text: str) -> bool:
        """Return True if `text` (full PDF text) belongs to this parser."""

    @abstractmethod
    def parse(self, pdf_path: Path) -> ParsedStatement:
        """Parse the PDF and return a fully-populated ParsedStatement."""

    @staticmethod
    def extract_text(pdf_path: Path) -> str:
        """Concatenate all pages of a PDF into one string."""
        pages: list[str] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text(x_tolerance=3, y_tolerance=3)
                if t:
                    pages.append(t)
        return "\n".join(pages)

    @staticmethod
    def parse_amount(raw: str) -> Optional[Decimal]:
        """
        Convert a raw string amount such as "$1,234.56" or "(234.56)" to Decimal.
        Parentheses denote negative values (accounting convention).
        """
        if not raw:
            return None
        raw = raw.strip()
        negative = raw.startswith("(") or raw.startswith("-")
        cleaned = re.sub(r"[()$, ]", "", raw)
        try:
            value = Decimal(cleaned)
            return -abs(value) if negative else abs(value)
        except InvalidOperation:
            return None

    @staticmethod
    def parse_date(raw: str, year: Optional[int] = None) -> Optional[date]:
        """
        Parse common date formats: MM/DD, MM/DD/YYYY, MM/DD/YY.
        If only MM/DD is supplied, `year` is used as the year.
        """
        if not raw:
            return None
        raw = raw.strip()
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m/%d", "%Y-%m-%d"):
            try:
                d = date(1900, 1, 1)  # placeholder
                import datetime as _dt
                d = _dt.datetime.strptime(raw, fmt).date()
                if fmt == "%m/%d" and year:
                    d = d.replace(year=year)
                return d
            except ValueError:
                continue
        return None

    @staticmethod
    def period_from_date(d: date) -> str:
        """Return 'YYYY-MM' for a date."""
        return d.strftime("%Y-%m")

    @staticmethod
    def mask_account(full_number: str) -> str:
        """Return last 4 digits of an account number."""
        cleaned = re.sub(r"\D", "", full_number)
        return cleaned[-4:] if len(cleaned) >= 4 else cleaned

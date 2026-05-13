"""
parsers/registry.py  –  Plugin registry for statement parsers
──────────────────────────────────────────────────────────────
Usage:
    @ParserRegistry.register
    class MyBankParser(BaseStatementParser):
        ...

    parser = ParserRegistry.detect(full_pdf_text)
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Type

from core.exceptions import ParserNotFoundError

class ParserRegistry:
    _registry: Dict[str, "Type"] = {}   # parser_id → class

    @classmethod
    def register(cls, parser_cls: "Type") -> "Type":
        """Class decorator – adds the parser to the registry."""
        pid = parser_cls.PARSER_ID
        if not pid:
            raise ValueError(f"{parser_cls.__name__} must define PARSER_ID")
        cls._registry[pid] = parser_cls
        return parser_cls

    @classmethod
    def detect(cls, text: str) -> Optional["Type"]:
        """
        Scan `text` against every registered parser's can_parse().
        Returns the matching class, or None if nothing matches.
        """
        for parser_cls in cls._registry.values():
            if parser_cls.can_parse(text):
                return parser_cls
        return None

    @classmethod
    def detect_or_raise(cls, text: str) -> "Type":
        parser_cls = cls.detect(text)
        if parser_cls is None:
            raise ParserNotFoundError(
                "No registered parser could handle this PDF. "
                "Add a custom parser in parsers/ and register it."
            )
        return parser_cls

    @classmethod
    def list_parsers(cls) -> List[str]:
        return list(cls._registry.keys())

    @classmethod
    def get(cls, parser_id: str) -> Optional["Type"]:
        return cls._registry.get(parser_id)

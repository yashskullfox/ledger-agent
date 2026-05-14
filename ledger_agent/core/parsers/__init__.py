"""
parsers  –  PDF statement parsers (plugin registry)

All parser modules in this package are auto-discovered at import time via
pkgutil.iter_modules so that adding a new bank requires only:
  1. Create parsers/your_bank.py with @ParserRegistry.register
  2. No other changes needed

Parsers that cannot be imported (e.g. pdfplumber not installed) are silently
skipped — the rest of the registry remains intact.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from ledger_agent.core.parsers.registry import ParserRegistry

_SKIP = {"base", "registry"}
_parsers_dir = Path(__file__).parent

for _mod_info in pkgutil.iter_modules([str(_parsers_dir)]):
    if _mod_info.name in _SKIP:
        continue
    try:
        importlib.import_module(f"ledger_agent.core.parsers.{_mod_info.name}")
    except Exception:
        pass  # optional dependency missing — parser silently unavailable

__all__ = ["ParserRegistry"]

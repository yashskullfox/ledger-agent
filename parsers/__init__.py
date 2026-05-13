"""parsers – PDF statement parsers (plugin registry).

Auto-discovery: every .py module in this package (except __init__, base,
and registry) is imported at package load time so its parser class
self-registers via ParserRegistry.register().  This means dropping a new
parser file into this directory is the ONLY step needed to activate it —
no changes to cli/commands.py, cli/quick_scan.py, or config.KNOWN_PARSERS.
"""
import importlib
import pkgutil
from pathlib import Path

from parsers.registry import ParserRegistry  # noqa: F401  (re-exported)

_SKIP = {"__init__", "base", "registry"}

# Walk every module in this package and import it.
# Each parser module calls ParserRegistry.register() at import time.
for _mod_info in pkgutil.iter_modules([str(Path(__file__).parent)]):
    if _mod_info.name not in _SKIP:
        importlib.import_module(f"parsers.{_mod_info.name}")

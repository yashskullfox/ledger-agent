"""
tests/architecture/test_core_purity.py  –  ARCH-02 / ARCH-20 core purity guardrail
====================================================================================

Enforces that ledger_agent.core never imports:
  - cli.*            (command-line interface)
  - rich             (console rendering library)
  - click / typer    (CLI frameworks)
  - requests / httpx (outbound HTTP — all network must be opt-in)
  - fastapi / flask  (web frameworks — Form C uses mcp SDK, not these)

ARCH-20: The old top-level packages (core/, accounting/, intelligence/,
parsers/, reports/) have been physically moved under ledger_agent/core/.
The test now asserts those stale top-level directories do NOT exist so the
"carve-out" is real and not merely cosmetic.

The test walks every .py file under ledger_agent/core/ and inspects imports
using AST analysis, catching imports regardless of indentation or guards.

Run:
    pytest tests/architecture/test_core_purity.py -q
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ── Configuration ─────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent.parent

# ARCH-20: all core logic lives exclusively under ledger_agent/core/.
# The old top-level aliases (core/, accounting/, …) must no longer exist.
CORE_DIRS: list[Path] = [
    PROJECT_ROOT / "ledger_agent" / "core",
]

# ARCH-20 acceptance criterion: these top-level directories must be absent.
_BANNED_TOPLEVEL_DIRS: list[str] = [
    "core",
    "accounting",
    "intelligence",
    "parsers",
    "reports",
]

# Top-level module prefixes that must NOT appear in any core file
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "cli",        # CLI layer
    "rich",       # console rendering — use stdlib logging in core
    "click",      # CLI framework
    "typer",      # CLI framework
    "requests",   # outbound HTTP
    "httpx",      # outbound HTTP
    "fastapi",    # web framework
    "flask",      # web framework
    "questionary",# interactive prompts
    "colorama",   # terminal colour (acceptable in cli, not core)
)

# Files explicitly excluded from the check (e.g. logging_setup uses Rich for
# format definition only; the formatter is selected at runtime by CLI layer)
EXCLUDED_FILES: frozenset[str] = frozenset({
    "logging_setup.py",   # Rich logging formatter — acceptable shim
    "renderer.py",        # reports/renderer.py is a presentation layer
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _collect_py_files(dirs: list[Path]) -> list[Path]:
    files = []
    for d in dirs:
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.py")):
            if f.name not in EXCLUDED_FILES and "__pycache__" not in f.parts:
                files.append(f)
    return files


def _get_top_level_imports(source: str) -> list[str]:
    """Return the list of top-level module names imported in *source*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    top_levels = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_levels.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_levels.append(node.module.split(".")[0])
    return top_levels


def _check_file(path: Path) -> list[str]:
    """Return list of violations found in *path*."""
    source = path.read_text(encoding="utf-8", errors="replace")
    imports = _get_top_level_imports(source)
    violations = []
    for imp in imports:
        if imp.startswith(FORBIDDEN_PREFIXES):
            violations.append(
                f"{path.relative_to(PROJECT_ROOT)}: forbidden import '{imp}'"
            )
    return violations


# ── Parametrized test ─────────────────────────────────────────────────────────

_all_files = _collect_py_files(CORE_DIRS)


@pytest.mark.parametrize("py_file", _all_files, ids=lambda p: str(p.relative_to(PROJECT_ROOT)))
def test_core_file_has_no_forbidden_imports(py_file: Path) -> None:
    """Each core .py file must not import forbidden UI/network modules."""
    violations = _check_file(py_file)
    assert violations == [], (
        "Core purity violation(s) found:\n" + "\n".join(violations) + "\n\n"
        "ledger_agent/core/ must never import cli.*, rich, click, typer,\n"
        "requests, httpx, fastapi, flask, questionary, or colorama.\n"
        "Move the offending import to the cli/ or mcp/ layer instead."
    )


def test_core_has_no_forbidden_imports() -> None:
    """Aggregate gate: collect all violations and fail once with full list.

    This is the accept-check function referenced in ARCH-02:
        pytest tests/architecture/test_core_purity.py::test_core_has_no_forbidden_imports -q
    """
    all_violations: list[str] = []
    for py_file in _all_files:
        all_violations.extend(_check_file(py_file))

    assert all_violations == [], (
        f"{len(all_violations)} core purity violation(s):\n"
        + "\n".join(all_violations)
    )


def test_arch20_old_toplevel_packages_removed() -> None:
    """ARCH-20 acceptance criterion: the old top-level package directories must
    not exist.  All core logic has been physically moved into ledger_agent/core/.
    If any of these directories are present the package carve-out is incomplete.
    """
    still_present = [
        d for d in _BANNED_TOPLEVEL_DIRS
        if (PROJECT_ROOT / d).is_dir()
    ]
    assert still_present == [], (
        "ARCH-20 violation: the following top-level directories still exist and "
        "must be removed (all core code now lives under ledger_agent/core/):\n"
        + "\n".join(f"  {PROJECT_ROOT / d}" for d in still_present)
    )

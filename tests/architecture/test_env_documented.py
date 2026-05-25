"""
tests/architecture/test_env_documented.py  –  All env vars must be in docs/env-vars.md (W24)
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_VARS_DOC = ROOT / "docs" / "env-vars.md"

# Vars that are allowed to be undocumented (test-only overrides, CI vars, etc.)
_EXEMPT = frozenset({
    "FI_CPA_CORPUS_PATH",  # test-only override for parity corpus path
    "FI_DB_PATH",          # already documented
})


def _find_fi_vars_in_source() -> set[str]:
    """Scan all Python source files for os.environ.get('FI_*') calls."""
    pattern = re.compile(r"""os\.environ\.get\s*\(\s*['"](\bFI_[A-Z_0-9]+\b)""")
    found: set[str] = set()
    for py_file in ROOT.rglob("*.py"):
        parts = py_file.parts
        if "private" in parts or ".venv" in parts:
            continue
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
            found.update(pattern.findall(text))
        except OSError:
            pass
    return found


def _vars_in_docs() -> set[str]:
    """Extract env var names mentioned in docs/env-vars.md."""
    if not ENV_VARS_DOC.exists():
        return set()
    text = ENV_VARS_DOC.read_text(encoding="utf-8")
    return set(re.findall(r"`(FI_[A-Z_0-9]+)`", text))


class TestEnvVarsDocumented:
    def test_docs_file_exists(self):
        assert ENV_VARS_DOC.exists(), (
            f"docs/env-vars.md not found — create it (W24)"
        )

    def test_all_fi_vars_documented(self):
        source_vars = _find_fi_vars_in_source()
        doc_vars = _vars_in_docs()
        undocumented = (source_vars - doc_vars) - _EXEMPT
        assert not undocumented, (
            f"The following FI_* env vars are used in source but NOT documented "
            f"in docs/env-vars.md:\n  {sorted(undocumented)}\n"
            f"Add entries to docs/env-vars.md (W24)."
        )

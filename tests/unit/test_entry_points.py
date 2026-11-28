"""
tests/unit/test_entry_points.py  –  X-03: the installed entry points work outside the repo

The ``ledger`` console script used to fail with ImportError: ``ledger_agent/cli/main.py``
did ``from config import …``, which only resolves to the root ``config.py`` shim when
the repo root is on ``sys.path``. A wheel does not ship that shim (and an editable
install resolved ``config`` to the ``config/`` directory instead).

(a) the CLI and MCP entry points answer ``--help`` from a temp cwd, both via
    ``python -m`` and via the installed console scripts (skipped when not installed);
(b) every module of the package imports from a copy of ``ledger_agent/`` alone (what a
    wheel ships), with the repo root off ``sys.path``, and never loads a top-level
    ``config`` (this is the check that runs in CI, where nothing is pip-installed);
(c) static guard: no module in ``ledger_agent/`` imports the root ``config`` shim.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "ledger_agent"

_TIMEOUT = 120


def _clean_env(tmp_path: Path, pythonpath: Path | None = None) -> dict[str, str]:
    """Subprocess env: no inherited PYTHONPATH; all FI_* state under ``tmp_path``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("FI_")}
    env.pop("PYTHONPATH", None)
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    data = tmp_path / "fi_data"
    env.update(
        {
            "FI_DATA_DIR": str(data),
            "FI_DB_PATH": str(data / "test.db"),
            "FI_MEMORY_FILE": str(data / "memory.json"),
            "FI_AUDIT_DIR": str(data / "audit"),
            "FI_AUDIT_DISABLED": "1",
            "FI_AI_BACKEND": "local",
            "FI_LOG_LEVEL": "WARNING",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def _run(cmd: list[str], tmp_path: Path, pythonpath: Path | None = None):
    workdir = tmp_path / "cwd"
    workdir.mkdir(exist_ok=True)
    return subprocess.run(
        cmd,
        cwd=workdir,
        env=_clean_env(tmp_path, pythonpath),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_TIMEOUT,
    )


def _console_script(name: str) -> Path | None:
    """Path of an installed console script for this interpreter, or None."""
    candidates = []
    for scripts_dir in {sysconfig.get_path("scripts"), str(Path(sys.executable).parent)}:
        base = Path(scripts_dir)
        candidates += [base / name, base / f"{name}.exe"]
    for c in candidates:
        if c.is_file():
            return c
    return None


# ── (a) --help from a temp cwd ────────────────────────────────────────────────

@pytest.mark.parametrize("module", ["ledger_agent.cli.main", "ledger_agent.mcp.server"])
def test_python_m_help_from_temp_cwd(module, tmp_path):
    proc = _run([sys.executable, "-m", module, "--help"], tmp_path, pythonpath=REPO_ROOT)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "options" in proc.stdout.lower()  # custom CLI help and argparse both list options


@pytest.mark.parametrize("script", ["ledger", "ledger-agent-mcp"])
def test_installed_console_script_help(script, tmp_path):
    path = _console_script(script)
    if path is None:
        pytest.skip(f"{script} console script not installed for this interpreter")
    proc = _run([str(path), "--help"], tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "options" in proc.stdout.lower()  # custom CLI help and argparse both list options


# ── (b) import from the package alone (wheel layout) ──────────────────────────

# Imports the three entry modules, then every module in the package (several entry
# paths import lazily, e.g. mcp/server.py -> mcp/tools.py only on first call).
# A missing *third-party* optional dependency is tolerated; a missing or broken
# first-party / repo-root module (config, adapters, mcp_server, main, ...) is not.
_IMPORT_PROBE = r"""
import importlib
import pkgutil
import sys
from pathlib import Path

site, repo = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
assert all(Path(p or ".").resolve() != repo for p in sys.path), "repo root on sys.path"

import ledger_agent.bridge.jsonrpc_stdio
import ledger_agent.cli.main
import ledger_agent.mcp.server

pkg = Path(ledger_agent.__file__).resolve()
assert site in pkg.parents, f"ledger_agent imported from outside the wheel copy: {pkg}"

repo_top = {"config", "main", "adapters", "mcp_server", "private", "scripts", "ledger_agent"}
bad = []
for info in pkgutil.walk_packages(ledger_agent.__path__, "ledger_agent."):
    if info.name.endswith(".__main__"):  # runs the server on import
        continue
    try:
        importlib.import_module(info.name)
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] in repo_top:
            bad.append(f"{info.name}: {exc!r}")
    except Exception as exc:
        bad.append(f"{info.name}: {exc!r}")
assert not bad, "\n".join(bad)
assert "config" not in sys.modules, "a top-level 'config' module was imported"
print("OK")
"""


def test_package_imports_without_repo_root(tmp_path):
    site = tmp_path / "site"
    shutil.copytree(
        PACKAGE_DIR,
        site / "ledger_agent",
        # ledger_agent/data holds local run output (gitignored), not package code.
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "data"),
    )
    proc = _run(
        [sys.executable, "-c", _IMPORT_PROBE, str(site), str(REPO_ROOT)],
        tmp_path,
        pythonpath=site,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout.strip().endswith("OK")


# ── (c) static guard ──────────────────────────────────────────────────────────

def _root_config_imports(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "config" or node.module.startswith("config."):
                hits.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == "config" or a.name.startswith("config.") for a in node.names):
                hits.append(node.lineno)
    return hits


def test_no_package_module_imports_root_config_shim():
    offenders = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for line in _root_config_imports(path):
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}")
    assert not offenders, (
        "import from ledger_agent.core.config, not the root config shim: "
        + ", ".join(offenders)
    )


def test_static_guard_detects_root_config_import(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import config\n"
        "from config import DB_PATH\n"
        "from ledger_agent.core.config import DB_PATH as OK\n"
        "from ledger_agent.core import config as fine\n"
        "def f():\n"
        "    from config import AI_BACKEND\n",
        encoding="utf-8",
    )
    assert _root_config_imports(sample) == [1, 2, 6]

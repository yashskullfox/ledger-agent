"""P-10: the ``http`` optional-dependency group provides uvicorn for the MCP
HTTP transport, and ``all`` includes it too."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


@pytest.fixture(scope="module")
def extras() -> dict[str, list[str]]:
    if tomllib is None:
        pytest.skip("tomllib/tomli not available")
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["optional-dependencies"]


def _names(reqs: list[str]) -> set[str]:
    return {re.split(r"[\s<>=!~;\[]", r, maxsplit=1)[0].lower() for r in reqs}


def test_http_extra_provides_uvicorn(extras):
    assert "http" in extras
    assert "uvicorn" in _names(extras["http"])


def test_http_extra_pins_a_floor(extras):
    (req,) = [r for r in extras["http"] if r.lower().startswith("uvicorn")]
    assert ">=" in req


def test_all_extra_includes_uvicorn(extras):
    assert "uvicorn" in _names(extras["all"])

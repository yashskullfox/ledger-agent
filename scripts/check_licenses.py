#!/usr/bin/env python3
"""
scripts/check_licenses.py  –  Third-party licence audit (W25)
=============================================================
Checks that all installed packages have permissive (non-GPL) licences.
Exits 0 if all packages are permissive, 1 if any GPL/LGPL packages found.

Usage::

    pip install pip-licenses
    python scripts/check_licenses.py
    python scripts/check_licenses.py --requirements requirements.txt
"""
from __future__ import annotations

import subprocess
import sys

# Licences that are considered acceptable for distribution
PERMISSIVE = frozenset({
    "MIT", "MIT License",
    "BSD", "BSD License", "BSD-2-Clause", "BSD-3-Clause",
    "Apache Software License", "Apache 2.0", "Apache License 2.0",
    "ISC", "ISC License", "ISC License (ISCL)",
    "Python Software Foundation License",
    "HPND", "Historical Permission Notice and Disclaimer (HPND)",
    "Public Domain",
    "CC0 1.0 Universal (CC0 1.0) Public Domain Dedication",
    "The Unlicense (Unlicense)",
    "Mozilla Public License 2.0 (MPL 2.0)",
})

# Known exceptions that need review
GPL_LIKE = frozenset({
    "GNU General Public License v2 (GPLv2)",
    "GNU General Public License v3 (GPLv3)",
    "GNU General Public License v2 or later (GPLv2+)",
    "GNU General Public License v3 or later (GPLv3+)",
    "GNU Lesser General Public License v2 (LGPLv2)",
    "GNU Lesser General Public License v2 or later (LGPLv2+)",
    "GNU Lesser General Public License v3 (LGPLv3)",
    "GNU Affero General Public License v3 (AGPLv3)",
})


def main() -> int:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip_licenses", "--format=csv", "--order=license"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("pip-licenses not installed. Run: pip install pip-licenses")
        return 1

    if result.returncode != 0:
        print(f"pip-licenses failed: {result.stderr}")
        return 1

    import csv
    import io

    reader = csv.DictReader(io.StringIO(result.stdout))
    rows = list(reader)

    blockers = []
    warnings = []
    for row in rows:
        lic = row.get("License", "UNKNOWN").strip()
        pkg = row.get("Name", "?")
        if any(g in lic for g in GPL_LIKE) or "GPL" in lic:
            blockers.append(f"  {pkg}: {lic}")
        elif lic not in PERMISSIVE and lic != "UNKNOWN":
            warnings.append(f"  {pkg}: {lic}")

    print(f"Scanned {len(rows)} package(s).")

    if warnings:
        print(f"\nWARN: {len(warnings)} package(s) with unusual licences (manual review needed):")
        for w in warnings:
            print(w)

    if blockers:
        print(f"\nFAIL: {len(blockers)} package(s) with GPL-like licences (release blocker):")
        for b in blockers:
            print(b)
        return 1

    print("\nPASS: All packages have permissive licences.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

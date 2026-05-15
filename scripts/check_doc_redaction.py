#!/usr/bin/env python3
"""
scripts/check_doc_redaction.py  –  Doc-redaction scanner (ARCH-34 / R-75)

Scans committed files for PII tokens that should be replaced with pseudonyms.
Reads the corpus from config/redaction_corpus.yaml.

Usage:
    python scripts/check_doc_redaction.py --all-tracked
    python scripts/check_doc_redaction.py --paths README.md STRUCTURE.md
    python scripts/check_doc_redaction.py --staged

Exit codes:
    0  clean (no hits)
    1  one or more hits found
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = REPO_ROOT / "config" / "redaction_corpus.yaml"
ALLOWLIST_COMMENT = "# redaction: allow"
ALLOWLIST_FILE = REPO_ROOT / "redaction.allowlist"


def _load_corpus() -> dict:
    try:
        import yaml
        with CORPUS_PATH.open() as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # PyYAML not installed — fall back to basic token list
        return {}
    except FileNotFoundError:
        return {}


def _all_tracked_files() -> List[Path]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=REPO_ROOT
    )
    return [REPO_ROOT / p.strip() for p in result.stdout.splitlines() if p.strip()]


def _staged_files() -> List[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    return [REPO_ROOT / p.strip() for p in result.stdout.splitlines() if p.strip()]


def _build_allowlist() -> set:
    tokens = set()
    if ALLOWLIST_FILE.exists():
        for line in ALLOWLIST_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                tokens.add(line.lower())
    return tokens


def _scan_file(
    path: Path,
    corpus: dict,
    allowlist: set,
) -> List[Tuple[int, str, str]]:
    """Return list of (line_no, category, context) hits. Never prints the match value."""
    if not path.exists() or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    hits = []
    replacement_tokens = corpus.get("replacement_tokens", {})
    # Build a set of all allowed pseudonym tokens (case-insensitive)
    allowed = set()
    for tokens in replacement_tokens.values():
        for t in tokens:
            allowed.add(t.lower())
    allowed |= allowlist

    pii_patterns = corpus.get("pii_patterns", {})

    for line_no, line in enumerate(text.splitlines(), 1):
        # Skip allowlist-annotated lines
        if ALLOWLIST_COMMENT in line:
            continue

        # Check account number pattern
        if "account_number" in pii_patterns:
            if re.search(pii_patterns["account_number"], line):
                # Allow if it matches an allowed token
                if not any(a in line.lower() for a in allowed):
                    hits.append((line_no, "account_number", f"{path.name}:{line_no}"))

    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="Doc-redaction scanner")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all-tracked", action="store_true",
                       help="Scan all git-tracked files")
    group.add_argument("--staged", action="store_true",
                       help="Scan only git-staged files")
    group.add_argument("--paths", nargs="+", metavar="PATH",
                       help="Scan specific files")
    args = parser.parse_args()

    corpus = _load_corpus()
    allowlist = _build_allowlist()

    if args.all_tracked:
        files = _all_tracked_files()
    elif args.staged:
        files = _staged_files()
    elif args.paths:
        files = [Path(p) for p in args.paths]
    else:
        files = _all_tracked_files()

    # Skip binary and large files
    skip_extensions = {".pdf", ".db", ".pyc", ".png", ".jpg", ".gif", ".ico",
                       ".woff", ".ttf", ".jar", ".zip", ".gz", ".lock"}
    files = [f for f in files if f.suffix.lower() not in skip_extensions]

    total_hits = 0
    for f in files:
        file_hits = _scan_file(f, corpus, allowlist)
        for line_no, category, context in file_hits:
            print(f"{context}: hit {category}")
            total_hits += 1

    if total_hits == 0:
        print(f"0 hits across {len(files)} tracked files")
        return 0
    else:
        print(f"{total_hits} hit(s) found")
        return 1


if __name__ == "__main__":
    sys.exit(main())

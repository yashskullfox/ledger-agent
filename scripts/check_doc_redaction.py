#!/usr/bin/env python3
"""
scripts/check_doc_redaction.py  –  Doc-redaction scanner (ARCH-34 / R-75)

Scans committed files for PII tokens that should be replaced with pseudonyms,
and for real-name denylist tokens that must never appear in committed files.
Reads the corpus from config/redaction_corpus.yaml.

Usage:
    python scripts/check_doc_redaction.py --all-tracked
    python scripts/check_doc_redaction.py --paths README.md STRUCTURE.md
    python scripts/check_doc_redaction.py --staged
    python scripts/check_doc_redaction.py --strict --all-tracked
    python scripts/check_doc_redaction.py --denylist-file path/to/extra.txt --paths ...

Exit codes:
    0  clean (no hits)
    1  one or more hits found
    2  configuration error (missing PyYAML, missing corpus, --strict with empty denylist)
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
DEFAULT_DENYLIST_LOCAL = REPO_ROOT / "private" / "denylist.local.txt"


def _load_corpus() -> dict:
    try:
        import yaml
    except ImportError:
        print(
            "FATAL: PyYAML not installed; cannot enforce R-73 scanner",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        with CORPUS_PATH.open() as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(
            f"FATAL: corpus file not found at {CORPUS_PATH}; cannot enforce R-73 scanner",
            file=sys.stderr,
        )
        sys.exit(2)


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


def _load_denylist(corpus: dict, override_path: Path | None = None) -> List[Tuple[str, str]]:
    """Return list of (category, token_lower) pairs from corpus + optional local file."""
    pairs: List[Tuple[str, str]] = []
    denylist = corpus.get("denylist_tokens", {}) or {}
    for category, tokens in denylist.items():
        if not tokens:
            continue
        for t in tokens:
            if t:
                pairs.append((str(category), str(t).lower()))

    local_path = override_path if override_path is not None else DEFAULT_DENYLIST_LOCAL
    if local_path.exists() and local_path.is_file():
        try:
            for line in local_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pairs.append(("entity", line.lower()))
        except OSError:
            pass
    return pairs


def _scan_file(
    path: Path,
    corpus: dict,
    allowlist: set,
    denylist: List[Tuple[str, str]],
) -> List[Tuple[int, str, str]]:
    """Return list of (line_no, category, context) hits. Never prints the match value."""
    if not path.exists() or not path.is_file():
        return []

    # Self-exempt the corpus file so denylist tokens don't trigger hits against
    # the very file that declares them.
    try:
        if path.resolve() == CORPUS_PATH.resolve():
            return []
    except OSError:
        pass

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    hits: List[Tuple[int, str, str]] = []
    replacement_tokens = corpus.get("replacement_tokens", {}) or {}
    # Build a set of all allowed pseudonym tokens (case-insensitive)
    allowed = set()
    for tokens in replacement_tokens.values():
        for t in tokens:
            allowed.add(t.lower())
    allowed |= allowlist

    pii_patterns = corpus.get("pii_patterns", {}) or {}
    # Pre-compile patterns (case-insensitive)
    compiled_patterns: List[Tuple[str, "re.Pattern"]] = []
    for category, pattern in pii_patterns.items():
        try:
            compiled_patterns.append((category, re.compile(pattern, re.IGNORECASE)))
        except re.error:
            continue

    for line_no, line in enumerate(text.splitlines(), 1):
        # Skip allowlist-annotated lines
        if ALLOWLIST_COMMENT in line:
            continue

        lower_line = line.lower()

        # Run every PII pattern; suppress if any allowlisted pseudonym appears on the line.
        for category, regex in compiled_patterns:
            if regex.search(line):
                if not any(a in lower_line for a in allowed):
                    hits.append((line_no, category, f"{path.name}:{line_no}"))

        # Denylist token check: any real-name substring match → hit.
        for category, token in denylist:
            if token and token in lower_line:
                hits.append(
                    (line_no, f"denylist:{category}", f"{path.name}:{line_no}")
                )

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
    parser.add_argument("--strict", action="store_true",
                        help="Require a non-empty denylist; exit 2 otherwise")
    parser.add_argument("--denylist-file", metavar="PATH", default=None,
                        help="Override path to the local denylist file "
                             "(default: private/denylist.local.txt)")
    args = parser.parse_args()

    corpus = _load_corpus()
    allowlist = _build_allowlist()

    override_path = Path(args.denylist_file) if args.denylist_file else None
    denylist = _load_denylist(corpus, override_path=override_path)

    if args.strict:
        denylist_section = corpus.get("denylist_tokens", {}) or {}
        total_tokens = sum(len(v or []) for v in denylist_section.values())
        # Include local-file tokens in the strict check too.
        total_tokens += sum(1 for cat, _ in denylist if cat == "entity") \
            - len(denylist_section.get("entity", []) or [])
        if len(denylist_section) == 0 or total_tokens <= 0:
            # Fall back: if local file added entries, denylist may still be non-empty.
            if len(denylist) == 0:
                print(
                    "FATAL: --strict requires a non-empty denylist",
                    file=sys.stderr,
                )
                return 2

    if args.all_tracked:
        files = _all_tracked_files()
    elif args.staged:
        files = _staged_files()
    elif args.paths:
        files = [Path(p) if Path(p).is_absolute() else (REPO_ROOT / p)
                 for p in args.paths]
    else:
        files = _all_tracked_files()

    # Skip binary and large files
    skip_extensions = {".pdf", ".db", ".pyc", ".png", ".jpg", ".gif", ".ico",
                       ".woff", ".ttf", ".jar", ".zip", ".gz", ".lock"}
    files = [f for f in files if f.suffix.lower() not in skip_extensions]

    # Skip files explicitly exempted by the corpus (verbatim third-party text,
    # licenses, vendored content where line-level allow comments aren't viable).
    exempt_paths = set()
    for rel in corpus.get("exempt_files", []) or []:
        try:
            exempt_paths.add((REPO_ROOT / rel).resolve())
        except OSError:
            pass
    if exempt_paths:
        def _is_exempt(p: Path) -> bool:
            try:
                return p.resolve() in exempt_paths
            except OSError:
                return False
        files = [f for f in files if not _is_exempt(f)]

    total_hits = 0
    for f in files:
        file_hits = _scan_file(f, corpus, allowlist, denylist)
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

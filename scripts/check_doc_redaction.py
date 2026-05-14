#!/usr/bin/env python3
"""
scripts/check_doc_redaction.py  –  Doc-redaction scanner (ARCH-34 / R-75)
==========================================================================

Scans committed / staged artefacts for PII that must not appear in the repo
per the redaction policy in ``config/redaction_corpus.yaml``.

Every hit is printed as::

    path:line:col: hit <category>

The matched value is **NEVER** printed (the scanner output is itself safe to
post in a PR comment).

Usage
-----
    # Staged files only (fast; used by pre-commit hook):
    python scripts/check_doc_redaction.py --staged

    # All tracked files:
    python scripts/check_doc_redaction.py --all-tracked

    # Specific paths:
    python scripts/check_doc_redaction.py --paths docs/ config/ README.md

    # Verbose (print summary even on clean run):
    python scripts/check_doc_redaction.py --all-tracked -v

Exit codes
----------
  0  No hits (or all suppressed by allow-list)
  1  One or more hits found
  2  Configuration error (corpus not found, YAML parse failure)

Allow-listing
-------------
  Inline: append ``  # redaction: allow`` to the line
  File:   list paths in ``redaction.allowlist`` (one per line, repo-relative)

Performance
-----------
  Target: < 5 s on the full tracked file set (R-75).
  All regex patterns are pre-compiled once at startup.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORPUS_PATH = _REPO_ROOT / "config" / "redaction_corpus.yaml"
_ALLOWLIST_PATH = _REPO_ROOT / "redaction.allowlist"

# File extensions scanned (binary files skipped automatically)
_SCAN_EXTENSIONS = {
    ".md", ".txt", ".rst", ".yaml", ".yml", ".json", ".sql",
    ".py", ".java", ".ts", ".js", ".sh", ".toml", ".ini", ".cfg",
    ".html", ".xml", ".csv",
}

# Lines containing this suffix are suppressed
_INLINE_ALLOW = "# redaction: allow"

# ---------------------------------------------------------------------------
# Hit record
# ---------------------------------------------------------------------------


class Hit(NamedTuple):
    path: str
    line: int
    col: int
    category: str
    # matched value is intentionally NOT stored — scanner output stays clean


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def _load_corpus(corpus_path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        print(
            "[ERROR] PyYAML not installed. Install with: pip install pyyaml",
            file=sys.stderr,
        )
        sys.exit(2)

    if not corpus_path.exists():
        print(
            f"[ERROR] Corpus not found: {corpus_path}\n"
            "Create config/redaction_corpus.yaml or set --corpus.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        with corpus_path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as exc:
        print(f"[ERROR] Failed to parse corpus: {exc}", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# Allow-list loading
# ---------------------------------------------------------------------------


def _load_allowlist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    result: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            result.add(line)
    return result


# ---------------------------------------------------------------------------
# Pattern compilation
# ---------------------------------------------------------------------------


class ScannerRules:
    """Pre-compiled patterns built from the corpus."""

    def __init__(self, corpus: dict) -> None:
        self._token_patterns: list[tuple[re.Pattern, str]] = []
        self._account_patterns: list[tuple[re.Pattern, str, bool]] = []
        self._path_patterns: list[tuple[re.Pattern, str]] = []
        self._financial_pattern: re.Pattern | None = None
        self._financial_nouns: list[str] = []
        self._financial_window: int = 5
        self._system_words: set[str] = set()
        self._replacement_tokens: set[str] = set()

        self._build(corpus)

    def _build(self, corpus: dict) -> None:
        # Replacement tokens — safe output, never flagged
        rt = corpus.get("replacement_tokens") or {}
        if isinstance(rt, dict):
            self._replacement_tokens = {v.strip() for v in rt.values() if isinstance(v, str)}

        # System words — always allowed
        sw = corpus.get("system_words") or []
        self._system_words = {w.upper() for w in sw if isinstance(w, str)}

        # Literal token patterns from entity / partner / bank / broker / ticker lists
        _singular = {
            "entities": "entity",
            "partners": "partner",
            "banks": "bank",
            "brokers": "broker",
            "tickers": "ticker",
        }
        for collection, singular in _singular.items():
            items = corpus.get(collection) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                real = item.get("real", "").strip()
                if real and real not in self._replacement_tokens:
                    try:
                        pat = re.compile(re.escape(real), re.IGNORECASE)
                        self._token_patterns.append((pat, singular))
                    except re.error:
                        pass

        # Account-number regex patterns
        for acct in corpus.get("account_number_patterns") or []:
            if not isinstance(acct, dict):
                continue
            pattern = acct.get("pattern", "")
            desc = acct.get("description", "account_number")
            ctx_exempt = bool(acct.get("context_exempt", True))
            try:
                self._account_patterns.append(
                    (re.compile(pattern), desc, ctx_exempt)
                )
            except re.error:
                pass

        # Path patterns
        for pp in corpus.get("path_patterns") or []:
            if not isinstance(pp, dict):
                continue
            pattern = pp.get("pattern", "")
            desc = pp.get("description", "path")
            try:
                self._path_patterns.append((re.compile(pattern), desc))
            except re.error:
                pass

        # Financial figure pattern
        ff = corpus.get("financial_figures") or {}
        if isinstance(ff, dict):
            fig_pat = ff.get("pattern", "")
            if fig_pat:
                try:
                    self._financial_pattern = re.compile(fig_pat)
                except re.error:
                    pass
            self._financial_nouns = [
                str(n) for n in (ff.get("proximity_nouns") or [])
            ]
            self._financial_window = int(ff.get("proximity_window", 5))


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" in chunk
    except OSError:
        return True


def _scan_file(path: Path, rules: ScannerRules) -> list[Hit]:
    if path.suffix.lower() not in _SCAN_EXTENSIONS:
        return []
    if _is_binary(path):
        return []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    hits: list[Hit] = []
    try:
        rel_path = str(path.relative_to(_REPO_ROOT))
    except ValueError:
        rel_path = str(path)
    lines = text.splitlines()

    for lineno, line in enumerate(lines, 1):
        # Inline allow-list suppression
        if _INLINE_ALLOW in line:
            continue

        # Skip lines that are just replacement tokens (documentation of the scheme)
        line_upper = line.upper()

        # 1. Literal token matches
        for pat, category in rules._token_patterns:
            for m in pat.finditer(line):
                hits.append(Hit(rel_path, lineno, m.start() + 1, category))

        # 2. Account-number patterns
        for pat, desc, ctx_exempt in rules._account_patterns:
            for m in pat.finditer(line):
                matched = m.group(0)
                # Skip last-4 masked patterns like ****1234
                if re.search(r'\*+\d{4}$', matched):
                    continue
                # Skip pure replacement tokens
                if matched.strip() in rules._replacement_tokens:
                    continue
                hits.append(Hit(rel_path, lineno, m.start() + 1, "account_number"))

        # 3. Path patterns
        for pat, desc in rules._path_patterns:
            for m in pat.finditer(line):
                hits.append(Hit(rel_path, lineno, m.start() + 1, "sensitive_path"))

        # 4. Financial figure proximity
        if rules._financial_pattern and rules._financial_nouns:
            for m in rules._financial_pattern.finditer(line):
                # Check if any financial noun appears within the proximity window
                # (approximated as character window, not strict token count)
                window_chars = 60  # ~5 tokens × 12 chars avg
                start = max(0, m.start() - window_chars)
                end = min(len(line), m.end() + window_chars)
                context = line[start:end].lower()
                for noun in rules._financial_nouns:
                    if noun.lower() in context:
                        # Only flag if not a replacement token itself
                        if m.group(0).strip() not in rules._replacement_tokens:
                            hits.append(
                                Hit(rel_path, lineno, m.start() + 1, "financial_figure")
                            )
                        break

    return hits


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------


def _collect_staged() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, check=True,
            cwd=_REPO_ROOT,
        )
        paths = []
        for line in result.stdout.splitlines():
            p = _REPO_ROOT / line.strip()
            if p.exists():
                paths.append(p)
        return paths
    except subprocess.CalledProcessError:
        return []


def _collect_tracked() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, check=True,
            cwd=_REPO_ROOT,
        )
        paths = []
        for line in result.stdout.splitlines():
            p = _REPO_ROOT / line.strip()
            if p.exists():
                paths.append(p)
        return paths
    except subprocess.CalledProcessError:
        return []


def _collect_paths(path_args: list[str]) -> list[Path]:
    paths: list[Path] = []
    for arg in path_args:
        p = Path(arg)
        if not p.is_absolute():
            p = _REPO_ROOT / p
        if p.is_dir():
            for child in p.rglob("*"):
                if child.is_file():
                    paths.append(child)
        elif p.exists():
            paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan committed artefacts for PII that should be redacted.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--staged", action="store_true",
        help="Scan only git-staged files (default mode for pre-commit hook).",
    )
    group.add_argument(
        "--all-tracked", action="store_true",
        help="Scan all git-tracked files.",
    )
    group.add_argument(
        "--paths", nargs="+", metavar="PATH",
        help="Scan specific files or directories.",
    )
    parser.add_argument(
        "--corpus", type=Path, default=_CORPUS_PATH,
        help=f"Path to redaction corpus YAML (default: {_CORPUS_PATH})",
    )
    parser.add_argument(
        "--allowlist", type=Path, default=_ALLOWLIST_PATH,
        help=f"Path to allow-list file (default: {_ALLOWLIST_PATH})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print summary even when no hits are found.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    corpus = _load_corpus(args.corpus)
    rules = ScannerRules(corpus)
    allowlist = _load_allowlist(args.allowlist)

    # Collect files to scan
    if args.staged:
        files = _collect_staged()
    elif args.all_tracked:
        files = _collect_tracked()
    elif args.paths:
        files = _collect_paths(args.paths)
    else:
        # Default: staged (hook mode)
        files = _collect_staged()

    # Filter allow-listed paths
    files = [
        f for f in files
        if str(f.relative_to(_REPO_ROOT) if f.is_absolute() else f) not in allowlist
    ]

    # Scan
    all_hits: list[Hit] = []
    for path in files:
        all_hits.extend(_scan_file(path, rules))

    # Report
    if all_hits:
        for hit in sorted(all_hits, key=lambda h: (h.path, h.line, h.col)):
            print(f"{hit.path}:{hit.line}:{hit.col}: hit {hit.category}")
        print(
            f"\n{len(all_hits)} hit(s) in {len({h.path for h in all_hits})} file(s). "
            "Fix by replacing real identifiers with pseudonyms from "
            "config/redaction_corpus.yaml.",
            file=sys.stderr,
        )
        return 1

    if args.verbose:
        print(
            f"[OK] Scanned {len(files)} file(s) — no redaction hits.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

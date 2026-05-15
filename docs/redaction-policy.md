# Doc-Redaction Policy

> Privacy notice: Pseudonyms used throughout: `ENTITY_A`, `PARTNER_1` / `PARTNER_2`,
> `BANK_X`, `BROKER_Y` / `BROKER_Z`. Real values live only in gitignored paths.

## Purpose

Prevent accidental leakage of partner names, ownership percentages, institution
brand strings, account numbers, and cent-precision financial figures in committed
artefacts (source, docs, test fixtures, CI config).

## Pseudonym corpus

The canonical pseudonym list lives in `config/redaction_corpus.yaml`. The mapping
from real identifiers to pseudonyms lives in `private/pseudonym-map.local.md`
(gitignored, never committed).

## Scanner

Run `python scripts/check_doc_redaction.py --all-tracked` to scan all tracked files.
The scanner exits non-zero on any hit. CI runs this on every PR.

## Rules

1. Real entity / partner / bank / broker / ticker names → corresponding pseudonym.
2. Cent-precision dollar figures within 5 tokens of a financial noun → `~$X,XXX`.
3. Account numbers → `acct_****` + last-4.
4. Ownership percentages in prose → `<P1_pct>` / `<P2_pct>`.

## Allowlist

Add `# redaction: allow` at the end of a line to suppress a specific hit.
Add path patterns to `redaction.allowlist` to suppress entire files.

## Pre-commit hook

After `make install-hooks`, every `git commit` runs the scanner against staged files.

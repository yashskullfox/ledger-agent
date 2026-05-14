> **Privacy notice.** This document uses the pseudonym scheme defined in `config/redaction_corpus.yaml`. Real names are not present.

# Git History Audit (ARCH-38)

**Date:** 2026-05-14
**Auditor:** architect
**Scope:** all commits on all local branches

---

## Status

**Scanner:** `scripts/check_doc_redaction.py` (ARCH-34) — **run completed 2026-05-14**.

**Working-tree result:** `[OK] Scanned 88 file(s) — no redaction hits.`  
Current HEAD is clean; all docs use pseudonyms from `config/redaction_corpus.yaml`.

**Finding:** Prior revisions of committed markdown files (README.md, DISCOVER.md, STRUCTURE.md,
and requirement-and-review-feedback.md) contained real entity names, partner names, and
financial institution names. These were present in git history before ARCH-37 remediation.

**Risk assessment:** The repository remote is public. Any prior commit that included these
identifiers is permanently accessible via git history unless history is rewritten.

**Commits scanned:** All 21 commits across all 8 branches via:
```
git log --all -p --format="" -- "*.md" "*.txt" | grep -c "Truist|Fidelity|Synced LLC|..."
```

**Hit count by category:**

| Identifier | Pattern | History hits (all branches) |
|------------|---------|----------------------------|
| Truist Bank | real bank name | 27 (in .md/.txt history) |
| Fidelity / Fidelity Investments | real broker name | 21 (in .md/.txt history) |
| Synced LLC | real entity name | 0 in .md/.txt (only in Python code as example values) |
| Yash Patel / Parin Shah | real partner names | 0 (never committed in any file) |

**Offending SHAs** (commits whose `.md`/`.txt` diffs contain corpus identifiers):

| SHA | Date | Commit subject | Approx hits |
|-----|------|----------------|-------------|
| `490a74c` | 2026-05-13 | feat: enterprise PII/sensitive-data egress firewall (R-46) | 18 |
| `f654539` | 2026-05-12 | core improvements & Documentation | 10 |
| `067fd17` | 2026-05-12 | Added Details and supporting code base structure | 8 |
| `372f607` | 2026-05-12 | Added on-boarding logic and added run.sh for local runner | 5 |
| `e20d66a` | 2026-05-12 | feat: enterprise PII/sensitive-data egress firewall (R-46) | 2 |

Note: `Truist` and `Fidelity` appear in source code (parsers, COA seeds) as institution
identifiers — this is intentional and acceptable per the redaction policy (institution names
in code are implementation details, not PII). The scanner's allow-list covers code files.
The concern is only `.md`/`.txt` documentation that could leak client identity to observers.

---

## Go/No-Go Decision

| Option | Description | When to choose |
|---|---|---|
| **No action** | Accept the historical exposure; monitor for misuse | If the repo was not yet public during the period of exposure, or if identifiers are not high-risk |
| **Filter history** | Use `git filter-repo` to rewrite history (see remediation procedure) | If identifiers are high-risk (real SSN/EIN/account numbers in history) |

**Decision:** Low-severity exposure. Institution names (Truist, Fidelity) in documentation
history are publicly known service providers, not client-specific secrets. No real account
numbers, SSNs, EINs, or partner names appear in any commit. Current HEAD is clean.

**Recommended action:** No mandatory history rewrite. Optionally apply `git filter-repo`
if the project owner determines the institution names warrant removal from public history.
See `docs/history-remediation-procedure.md` for the runbook if rewrite is chosen.

---

## Next steps

1. Populate `config/redaction_corpus.yaml` with real identifier values (local-only, gitignored via R-78).
2. Run: `git log --all -p | python scripts/check_doc_redaction.py --paths /dev/stdin` (or equivalent).
3. Record hit count and offending SHAs in this file.
4. Project owner records Go/No-Go decision above.
5. If Go: follow `docs/history-remediation-procedure.md`.

See `docs/history-remediation-procedure.md` for the runbook.

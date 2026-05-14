> **Privacy notice.** This document uses the pseudonym scheme defined in `config/redaction_corpus.yaml`. Real names are not present.

# Git History Remediation Procedure (ARCH-38)

This runbook covers the steps to rewrite git history to remove PII that was introduced in prior
commits. This is a destructive, coordinated operation. Read completely before starting.

---

## Prerequisites

1. `git filter-repo` installed: `pip install git-filter-repo`
2. All collaborators have been notified (see §4 — Collaborator coordination).
3. Go decision recorded in `docs/history-audit.md`.
4. A backup of the repo exists: `git clone --mirror <remote> repo-backup.git`

---

## Step 1 — Identify offending strings

```bash
# Run the scanner against full history (requires populated corpus)
python scripts/check_doc_redaction.py --all-tracked --verbose

# For history scan (pipe git log output):
git log --all -p -- '*.md' '*.txt' '*.yaml' '*.sql' > /tmp/history.patch
# Then manually search /tmp/history.patch for the real identifiers
# (use grep with actual values from private/pseudonym-map.local.md)
```

Record offending SHAs in `docs/history-audit.md`.

---

## Step 2 — Create replacements file for git filter-repo

Create `/tmp/replacements.txt` (never commit this file):

```
# Format: literal_string==>replacement_string
# Use actual values from private/pseudonym-map.local.md
REAL_ENTITY_NAME==>ENTITY_A
REAL_PARTNER_1_NAME==>PARTNER_1
REAL_PARTNER_2_NAME==>PARTNER_2
REAL_BANK_NAME==>BANK_X
```

---

## Step 3 — Dry run (verify the replacement)

```bash
# Clone a fresh copy for the dry run
git clone <remote> /tmp/repo-dry-run
cd /tmp/repo-dry-run

# Apply replacements (dry run — inspect output only)
git filter-repo --replace-text /tmp/replacements.txt --dry-run 2>&1 | head -100
```

Verify the replacements are correct before proceeding.

---

## Step 4 — Apply the rewrite

```bash
# Work on the clean clone
cd /tmp/repo-dry-run

# Apply replacements to all branches and tags
git filter-repo --replace-text /tmp/replacements.txt

# Verify: spot-check a few commits
git log --all --oneline | head -20
git show HEAD:README.md | head -5
```

---

## Step 5 — Force push (coordinated)

**This step is irreversible. All collaborators must re-clone.**

```bash
# Push rewritten history to remote
git push --force --all
git push --force --tags
```

---

## Step 6 — Collaborator coordination

Before force-pushing:

1. Notify all collaborators via Slack / email:
   > "NOTICE: We are rewriting git history on ledger-agent to remove PII from prior commits.
   > After [DATE/TIME], please delete your local clone and re-clone from the remote.
   > Any local branches must be re-based onto the new history. Contact [architect] with questions."

2. Freeze merges to main during the operation.
3. After push: confirm all collaborators have re-cloned.
4. Re-open any in-flight PRs against the new history.

---

## Step 7 — Credential rotation checklist

If any credentials (API keys, tokens, passwords) were present in history:

- [ ] Rotate affected API keys immediately (before/during history rewrite)
- [ ] Revoke old tokens at the provider
- [ ] Update secrets in GitHub Actions / CI
- [ ] Scan for downstream use of the compromised credentials

---

## Step 8 — Post-rewrite verification

```bash
# Run scanner on rewritten history
git log --all -p -- '*.md' | python scripts/check_doc_redaction.py --paths /dev/stdin 2>/dev/null || true

# Install pre-commit hook to prevent recurrence
make install-hooks
```

Record outcome in `docs/history-audit.md`.

---

## Contacts

- **Architect:** responsible for this procedure and the Go/No-Go decision
- **DevX:** pre-commit hook installation support
- **All collaborators:** must re-clone after force-push

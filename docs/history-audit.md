# Git History Redaction Audit

## Summary

History is **not publishable as-is**. The working tree is clean (scanner: 0 hits / 132 files), but git history across 45 commits and 11 local branches still contains every redacted token — Truist, Fidelity, Chase, BofA, Bank of America, US Bank/USBank, MONEYLINE, IBKR, Interactive Brokers, Yash, Parin, SYNCED — introduced as early as the initial commit. Owner must rewrite history before open-sourcing.

## Repo shape

- Total commits (reachable, all refs): 45
- `git rev-list --all --count`: 45
- Local branches: 11 (`main`, `development`, `Improvement-1`, `Improvement-buildFix`, `Improvement-buildFix-1`, `Improvement-buildFix-2`, `improvement-2..6`)
- Remote refs: `origin/main`, `origin/development`, `origin/Improvement-buildFix`, `origin/Improvement-buildFix-2`
- Topology: non-linear — multiple long-lived feature branches; rewrite must cover all refs, not just `main`.

## Token counts (last-200-commit `-G` window, `/tmp/history_dump.txt` = 29,342 lines)

| token | hit lines |
|---|---|
| truist | 254 |
| fidelity | 245 |
| chase | 112 |
| bofa | 85 |
| bank of america | 30 |
| usbank | 156 |
| us bank | 7 |
| moneyline | 40 |
| ibkr | 174 |
| interactive brokers | 46 |
| yash | 270 |
| parin | 255 |
| synced | 88 |

All thirteen tokens are present in history. `grep -ic` is case-insensitive, so counts cover both original and capitalized forms.

## Earliest exposures

| token group | commit | date | subject |
|---|---|---|---|
| Truist / Fidelity / Chase / Bank of America / MONEYLINE / SYNCED | `3c059d81bbdb3c696a0b1ea7173dd693f64e8d97` | 2026-05-12 20:17:16 -0500 | initial commit |
| USBank / IBKR / Interactive Brokers | `2f4fc5ae52401beb6b7c205709179f3b8899e3b5` | 2026-05-12 20:51:51 -0500 | feat: add US Bank + IBKR parsers, MCP server, enterprise code cleanup |
| Yash / Parin | `490a74c35469a55f4455bdfd2ba7612c8d3a095d` | 2026-05-13 16:18:42 -0500 | feat: enterprise PII/sensitive-data egress firewall (R-46) |

Bank tokens are present from commit #1. There is no clean prefix to publish.

## Recommended remediation

**Pick (b): squash to a single "Initial commit" before publishing.**

Justification:
- Tokens are seeded in the very first commit (`3c059d81`), so a `filter-repo` rewrite would have to touch every commit and every branch (45 commits across 11 local + 4 remote refs). Each rewritten commit must be re-reviewed to confirm no token leaked through near-miss patterns (e.g. `Tru1st`, `B0fA`, partial substrings in base64 fixtures or test snapshots).
- The repo has no external contributors and no published SHAs to preserve. Commit-history continuity has no value to outside consumers.
- Squashing collapses 45 commits and all branch divergence into one clean tree-snapshot of the already-clean working tree. Cost: O(1) review (just the final tree). Risk: near zero.
- `filter-repo` (option a) remains viable but is strictly more work for no external benefit; reserve it for the case where the owner wants to preserve authorship history of redacted commits, which is the opposite of the goal here.
- Option (c) is off the table — every token has > 0 hits.

Suggested sequence (owner runs in a fresh clone, not the working repo):

```
git clone --mirror <source> ledger-agent-publish.git
cd ledger-agent-publish.git
git checkout --orphan publish
git rm -rf .
# copy clean working tree from current repo HEAD
git add -A
git commit -m "Initial commit"
git branch -D <every other branch>
git tag -d $(git tag -l) 2>/dev/null
git push <new-public-remote> publish:main --force
```

If the owner ever decides option (a) is required instead, the equivalent `git filter-repo` invocation would be:

```
git filter-repo --replace-text <(printf '%s\n' \
  'Truist==>BankA' 'Fidelity==>BrokerA' 'Chase==>BankB' \
  'Bank of America==>BankC' 'BofA==>BankC' 'US Bank==>BankD' 'USBank==>BankD' \
  'MONEYLINE==>FEED1' 'Interactive Brokers==>BrokerB' 'IBKR==>BrokerB' \
  'Yash==>Dev1' 'Parin==>Dev2' 'SYNCED==>OK')
```

Run against a `--mirror` clone, then re-scan with the project scanner before publishing.

## What this audit did NOT check

- **Blobs in `.git/objects` outside the 200-commit `-G` window.** `git log -p -n 200` caps output; if any commit not surfaced by that pickaxe still contains a token, it was not counted. (With 45 total commits the window is not the limiter here, but the audit did not enumerate every blob via `git rev-list --objects --all | git cat-file --batch-check`.)
- **Force-deleted branches.** Anything pruned before `git gc` ran is unreachable; anything pruned and still within `gc.reflogExpire` is reachable via reflog only.
- **Stashes.** `git stash list` was not inspected.
- **Reflog entries** (`git reflog --all`). Local reflog can resurrect rewritten commits for 90 days by default; the rewrite plan above sidesteps this by publishing from a fresh orphan branch in a separate clone.
- **Tags.** No tags were enumerated (`git tag -l` was not run); if tags exist, they pin commits independent of branches and must be deleted in the rewrite.
- **Notes refs / replace refs / worktrees.** Not inspected.
- **Token variants not in the pattern set** (e.g. account numbers, routing numbers, email addresses, internal Walmart hostnames). This audit was scoped to the 13 named tokens only.

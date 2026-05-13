#!/usr/bin/env python3
"""
.github/scripts/compute_semver.py  —  Conventional-commit semver calculator
=============================================================================

Reads git log since the last ``vX.Y.Z`` tag and computes the next semver:

  feat!:  or  BREAKING CHANGE:  →  major bump
  feat:                          →  minor bump
  fix: / refactor: / chore: etc. →  patch bump

Usage::

    python .github/scripts/compute_semver.py
    # → writes "2.1.1" to $GITHUB_OUTPUT (key: version)
    # → prints the version to stdout

Environment
-----------
GITHUB_OUTPUT   Path to the GitHub Actions output file.
                If not set, version is printed to stdout only.

Exit codes
----------
0   Success
1   Could not determine version (no tags, no commits)
"""
import re
import subprocess
import sys
import os

_MAJOR_RE = re.compile(r"^(feat!|fix!|refactor!|BREAKING CHANGE)", re.MULTILINE)
_MINOR_RE = re.compile(r"^feat(\(.+?\))?:", re.MULTILINE)
_PATCH_RE = re.compile(r"^(fix|refactor|chore|perf|ci|build|docs|test)(\(.+?\))?:", re.MULTILINE)


def git(*args) -> str:
    result = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    return result.stdout.strip()


def last_tag() -> tuple[int, int, int] | None:
    tags = git("tag", "--list", "v*.*.*", "--sort=-v:refname").splitlines()
    for tag in tags:
        m = re.match(r"v?(\d+)\.(\d+)\.(\d+)$", tag.strip())
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def commits_since(tag: tuple[int, int, int] | None) -> str:
    if tag is None:
        return git("log", "--pretty=%s%n%b", "--no-merges")
    tag_str = f"v{tag[0]}.{tag[1]}.{tag[2]}"
    return git("log", f"{tag_str}..HEAD", "--pretty=%s%n%b", "--no-merges")


def bump(current: tuple[int, int, int], log: str) -> tuple[int, int, int]:
    major, minor, patch = current
    if _MAJOR_RE.search(log):
        return major + 1, 0, 0
    if _MINOR_RE.search(log):
        return major, minor + 1, 0
    if _PATCH_RE.search(log):
        return major, minor, patch + 1
    # No conventional commits found — patch bump anyway
    return major, minor, patch + 1


def main():
    tag = last_tag()
    if tag is None:
        current = (0, 1, 0)
    else:
        current = tag

    log = commits_since(tag)
    if not log.strip() and tag is not None:
        # No commits since last tag — output existing version
        version = f"{current[0]}.{current[1]}.{current[2]}"
    else:
        new = bump(current, log)
        version = f"{new[0]}.{new[1]}.{new[2]}"

    print(version)

    # Write to GitHub Actions output
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"version={version}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())

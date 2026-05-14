#!/usr/bin/env bash
# .github/scripts/sha256sums.sh  —  Generate SHA256SUMS for release artifacts
# ===========================================================================
# Usage:
#   sha256sums.sh <dir>
#
# Writes a SHA256SUMS file into <dir> containing one "hash  filename" line
# per artifact.  Compatible with both macOS (shasum -a 256) and Linux (sha256sum).
#
# Example:
#   sha256sums.sh dist/
#   cat dist/SHA256SUMS
#   a1b2c3... ledger-agent-core-v2.1.0.zip
#   d4e5f6... ledger-agent-cli-v2.1.0.tar.gz
#   ...

set -euo pipefail

DIR="${1:-.}"
OUTPUT="$DIR/SHA256SUMS"

if [ ! -d "$DIR" ]; then
    echo "ERROR: Directory not found: $DIR" >&2
    exit 1
fi

# Detect sha256sum vs shasum
if command -v sha256sum &>/dev/null; then
    SHA_CMD="sha256sum"
elif command -v shasum &>/dev/null; then
    SHA_CMD="shasum -a 256"
else
    echo "ERROR: neither sha256sum nor shasum found" >&2
    exit 1
fi

: > "$OUTPUT"

for artifact in "$DIR"/*.zip "$DIR"/*.tar.gz "$DIR"/*.jar; do
    [ -f "$artifact" ] || continue
    name=$(basename "$artifact")
    hash=$($SHA_CMD "$artifact" | cut -d' ' -f1)
    echo "$hash  $name" >> "$OUTPUT"
    echo "  $hash  $name"
done

echo ""
echo "SHA256SUMS written to $OUTPUT"

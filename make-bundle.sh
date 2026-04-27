#!/usr/bin/env bash
# Create a portable tarball of the benchmark for review.
# Excludes build artifacts, caches, the binary.
# Uses the script's own location so it works after a fresh git clone
# regardless of where the repo lives.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd -P)"
REPO_NAME="$(basename "${REPO_ROOT}")"
PARENT="$(dirname "${REPO_ROOT}")"

TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="/tmp/${REPO_NAME}-${TS}.tar.gz"

tar -czf "${OUT}" \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude="${REPO_NAME}/server/jaeger-mcp-bench-server" \
  -C "${PARENT}" \
  "${REPO_NAME}"

echo "wrote: ${OUT}"
ls -lh "${OUT}"
echo
echo "contents:"
tar -tzf "${OUT}" | wc -l
echo "files in bundle"

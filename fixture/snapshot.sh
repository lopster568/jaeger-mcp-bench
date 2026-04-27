#!/usr/bin/env bash
# Snapshot Prometheus storage so the benchmark can re-run against frozen metrics.
# Output: ../results/snapshots/<timestamp>/

set -euo pipefail

PROM_URL="${PROM_URL:-http://localhost:9090}"
OUT_DIR="${OUT_DIR:-../results/snapshots/$(date -u +%Y%m%dT%H%M%SZ)}"

mkdir -p "${OUT_DIR}"

echo "[snapshot] requesting Prometheus snapshot..."
RESP=$(curl -s -X POST "${PROM_URL}/api/v1/admin/tsdb/snapshot")
NAME=$(echo "${RESP}" | jq -r '.data.name')

if [[ -z "${NAME}" || "${NAME}" == "null" ]]; then
  echo "[snapshot] FAILED - Prometheus admin API may be disabled"
  echo "${RESP}"
  exit 1
fi

echo "[snapshot] snapshot name: ${NAME}"
echo "${NAME}" > "${OUT_DIR}/snapshot_name.txt"

# Copy snapshot out of the container.
docker cp "prometheus:/prometheus/snapshots/${NAME}" "${OUT_DIR}/"

# Capture metric counts as a quick sanity check.
echo "[snapshot] sanity: metric series counts"
curl -s "${PROM_URL}/api/v1/label/__name__/values" \
  | jq '.data | length' \
  > "${OUT_DIR}/metric_count.txt"

echo "[snapshot] saved to ${OUT_DIR}"

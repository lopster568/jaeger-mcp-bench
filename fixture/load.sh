#!/usr/bin/env bash
# Drive hotrod through its UI endpoints to generate spans + metrics.
# Three scenarios, selected by SCENARIO env var (default: degraded).
#
#   steady    - uniform 100 req/min
#   degraded  - currently identical to steady; error injection is a TODO
#               (hotrod's built-in latency/error behavior still applies)
#   spike     - ramp 100 -> 300 req/min at T=5min; error injection is a TODO
#
# Default duration: 10 minutes. Override with DURATION_SEC env var.

set -euo pipefail

SCENARIO="${SCENARIO:-degraded}"
DURATION_SEC="${DURATION_SEC:-600}"
HOTROD_URL="${HOTROD_URL:-http://localhost:8080}"

# Hotrod endpoints (per examples/hotrod/main.go):
#   /dispatch?customer={id}  - full customer→driver→route flow (heaviest)
#   /customer?customer={id}  - customer service only
#   /route?pickup={pt}&dropoff={pt} - route only
ENDPOINTS=(
  "/dispatch?customer=123"
  "/dispatch?customer=392"
  "/dispatch?customer=731"
  "/dispatch?customer=567"
  "/customer?customer=123"
  "/route?pickup=Lat:40.748,Long:-73.985&dropoff=Lat:40.7128,Long:-74.006"
)

end_at=$(( $(date +%s) + DURATION_SEC ))
total=0
errors=0

echo "[load] scenario=${SCENARIO} duration=${DURATION_SEC}s target=${HOTROD_URL}"

while [[ $(date +%s) -lt ${end_at} ]]; do
  # Rate logic per scenario (TODO: implement error injection)
  case "${SCENARIO}" in
    spike)
      now=$(date +%s)
      elapsed=$(( now - (end_at - DURATION_SEC) ))
      if [[ ${elapsed} -lt 300 ]]; then
        sleep_ms=600   # 100 req/min = 600ms cadence
      else
        sleep_ms=200   # 300 req/min = 200ms cadence
      fi
      ;;
    *)
      sleep_ms=600
      ;;
  esac

  ep="${ENDPOINTS[$RANDOM % ${#ENDPOINTS[@]}]}"
  if curl --max-time 10 -s -o /dev/null -w "" "${HOTROD_URL}${ep}"; then
    total=$((total + 1))
  else
    errors=$((errors + 1))
  fi

  # Sleep $sleep_ms milliseconds. Use perl for cross-platform float sleep.
  perl -e "select(undef, undef, undef, ${sleep_ms}/1000.0)"

  if (( total % 100 == 0 )); then
    echo "[load] requests=${total} errors=${errors} remaining=$(( end_at - $(date +%s) ))s"
  fi
done

echo "[load] done. requests=${total} errors=${errors}"

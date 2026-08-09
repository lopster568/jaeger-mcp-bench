#!/usr/bin/env bash
# Drive hotrod through its UI endpoints to generate spans + metrics.
# Three scenarios, selected by SCENARIO env var (default: degraded).
#
#   steady    - uniform 100 req/min
#   degraded  - currently identical to steady; error injection is a TODO
#               (hotrod's built-in latency/error behavior still applies)
#   spike     - ramp 100 -> 300 req/min at T=5min; error injection is a TODO
#   arm2      - fixed REQUEST COUNT instead of duration (default 80, override
#               with REQUEST_COUNT). The arm-2 fixture protocol caps load at
#               <=100 traces per task-relevant service (docs/arm2-design.md,
#               Fixture) so both arms can enumerate the full candidate set;
#               the ground-truth resolver hard-fails above the cap. Counting
#               requests, not seconds, is what actually bounds trace volume.
#
# Default duration: 10 minutes. Override with DURATION_SEC env var.

set -euo pipefail

SCENARIO="${SCENARIO:-degraded}"
DURATION_SEC="${DURATION_SEC:-600}"
REQUEST_COUNT="${REQUEST_COUNT:-80}"
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

if [[ "${SCENARIO}" == "arm2" ]]; then
  echo "[load] scenario=arm2 request_count=${REQUEST_COUNT} target=${HOTROD_URL}"
else
  echo "[load] scenario=${SCENARIO} duration=${DURATION_SEC}s target=${HOTROD_URL}"
fi

while true; do
  if [[ "${SCENARIO}" == "arm2" ]]; then
    [[ $(( total + errors )) -lt ${REQUEST_COUNT} ]] || break
  else
    [[ $(date +%s) -lt ${end_at} ]] || break
  fi
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
